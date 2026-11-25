"""Long-running scrape worker.

Every poll the worker:
  1. syncs the watch list: ships chem-os flags as `my_ship` (tracked as
     my-ships) and every ship in mainshipdata (tracked as other ships),
  2. scrapes whatever is due, one ship at a time, most urgent first:
     explicitly requested ships, then never-scraped ships (e.g. a new my-ship),
     then the most overdue ships. Refresh intervals: my-ships every
     MY_SHIP_INTERVAL_MINUTES (6h), other ships every OTHER_SHIP_INTERVAL_MINUTES
     (24h), and a ship last seen moored / at anchor waits at least
     STATIONARY_INTERVAL_MINUTES (10h).

Due ships are re-queried after every ship, so a newly added ship waits behind at
most the one scrape in progress, not a whole cycle.

Only one worker scrapes at a time (Postgres advisory lock); extra replicas wait
as hot standbys. Scrapes never exceed MAX_SHIPS_PER_HOUR, and if MarineTraffic
blocks several ships in a row the worker backs off exponentially.
"""
import logging
import os
import random
import threading
import time
from collections import deque

import psycopg2

from scraper import BlockedError, MarineTrafficClient, ScrapeError

log = logging.getLogger("tracker.worker")

BETWEEN_SHIPS = (3, 8)
BLOCK_COOLDOWN_BASE = 15 * 60
BLOCK_COOLDOWN_MAX = 6 * 3600


def make_client(settings):
    return MarineTrafficClient(timeout=settings.scraper_timeout, proxy=settings.scraper_proxy)


def write_heartbeat(path):
    try:
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(str(int(time.time())))
        os.replace(tmp, path)
    except OSError as e:
        log.warning("Cannot write heartbeat %s: %s", path, e)


def heartbeat_age(path):
    """Seconds since the last heartbeat, or None if there is none."""
    try:
        with open(path, encoding="utf-8") as f:
            return time.time() - int(f.read().strip())
    except (OSError, ValueError):
        return None


class Worker:
    def __init__(self, store, settings, stop=None, client_factory=make_client):
        self.store = store
        self.settings = settings
        self.stop = stop or threading.Event()
        self._client_factory = client_factory
        self._client = None
        self._lock_conn = None
        self._last_sync = 0.0
        self._consecutive_blocks = 0
        self._cooldowns = 0
        self._recent_scrapes = deque()
        self._cap_logged = False

    # ---- public entry points ----------------------------------------------

    def run_forever(self):
        s = self.settings
        log.info(
            "Worker starting: my-ships every %dm, others every %dm, stationary >= %dm, "
            "max %d ships/hour, poll %ds, sync=%s",
            s.my_ship_interval_minutes, s.other_ship_interval_minutes, s.stationary_interval_minutes,
            s.max_ships_per_hour, s.poll_seconds, s.sync_my_ships,
        )
        try:
            while not self.stop.is_set():
                self._beat()
                try:
                    if not self._ensure_lock():
                        self._wait(self.settings.poll_seconds)
                        continue
                    self._maybe_sync()
                    due = self.store.due_ships(
                        self.settings.my_ship_interval_minutes,
                        self.settings.other_ship_interval_minutes,
                        self.settings.stationary_interval_minutes,
                        limit=1,
                    )
                    if not due:
                        self._wait(self.settings.poll_seconds)
                        continue
                    if self._wait_for_rate_limit():
                        continue
                    self.scrape_one(due[0])
                    if self._consecutive_blocks >= self.settings.max_consecutive_blocks:
                        self._cool_down()
                    else:
                        self._wait(random.uniform(*BETWEEN_SHIPS))
                except psycopg2.Error as e:
                    log.error("Database error, retrying in %ds: %s", self.settings.poll_seconds, e)
                    self._release_lock()
                    self._wait(self.settings.poll_seconds)
                except Exception:
                    log.exception("Unexpected worker error, retrying in %ds", self.settings.poll_seconds)
                    self._wait(self.settings.poll_seconds)
        finally:
            self.close()
            log.info("Worker stopped")

    def run_once(self):
        """Scrapes every active ship once. Returns (ok, total), or None if another worker holds the lock."""
        if not self._ensure_lock():
            return None
        try:
            if self.settings.sync_my_ships:
                self._sync()
            ships = self.store.active_ships()
            if not ships:
                log.warning("Watch list is empty. Add ships with: python main.py add <ship_id>")
                return 0, 0
            ok = 0
            for i, ship_id in enumerate(ships):
                while self._wait_for_rate_limit():
                    if self.stop.is_set():
                        break
                if self.stop.is_set():
                    break
                ok += self.scrape_one(ship_id)
                if self._consecutive_blocks >= self.settings.max_consecutive_blocks:
                    log.error("Blocked by MarineTraffic on %d ships in a row; aborting run", self._consecutive_blocks)
                    break
                if i < len(ships) - 1:
                    self._wait(random.uniform(*BETWEEN_SHIPS))
            log.info("Done: %d/%d ships scraped", ok, len(ships))
            return ok, len(ships)
        finally:
            self.close()

    def scrape_one(self, ship_id):
        """Scrapes and stores one ship. Returns True on success; failures go to scrape_log."""
        if self._client is None:
            self._client = self._client_factory(self.settings)
        started = time.monotonic()
        self._recent_scrapes.append(started)
        try:
            data = self._client.fetch_vessel(ship_id)
            new_pos = self.store.save(ship_id, data)
        except BlockedError as e:
            self._consecutive_blocks += 1
            log.warning("%s blocked (%d in a row): %s", ship_id, self._consecutive_blocks, e)
            self.store.log_error(ship_id, e)
            return False
        except ScrapeError as e:
            self._consecutive_blocks = 0
            log.error("%s failed: %s", ship_id, e)
            self.store.log_error(ship_id, e)
            return False
        except psycopg2.Error:
            raise
        except Exception as e:
            log.exception("%s failed unexpectedly", ship_id)
            self.store.log_error(ship_id, e)
            return False
        self._consecutive_blocks = 0
        self._cooldowns = 0
        p = data["position"] or {}
        log.info(
            "%s %s | %s | lat=%s lon=%s spd=%s | %s | %.1fs",
            ship_id, data["general"].get("name"),
            p.get("navigationalStatus"), p.get("lat"), p.get("lon"), p.get("speed"),
            "NEW position" if new_pos else "position unchanged", time.monotonic() - started,
        )
        return True

    def close(self):
        if self._client is not None:
            self._client.close()
            self._client = None
        self._release_lock()

    # ---- internals ----------------------------------------------------------

    def _beat(self):
        write_heartbeat(self.settings.heartbeat_file)

    def _wait(self, seconds):
        """Sleeps up to `seconds`, waking early on shutdown and keeping the heartbeat fresh."""
        deadline = time.monotonic() + seconds
        while not self.stop.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            self.stop.wait(min(remaining, 30))
            self._beat()

    def _ensure_lock(self):
        if self._lock_conn is not None:
            try:
                with self._lock_conn.cursor() as cur:
                    cur.execute("SELECT 1")
                return True
            except psycopg2.Error:
                log.warning("Lost the worker lock connection; re-acquiring")
                self._release_lock()
        self._lock_conn = self.store.try_worker_lock()
        if self._lock_conn is None:
            log.info("Another worker holds the scrape lock; standing by")
            return False
        log.info("Acquired the worker lock")
        return True

    def _release_lock(self):
        if self._lock_conn is not None:
            try:
                self._lock_conn.close()
            except psycopg2.Error:
                pass
            self._lock_conn = None

    def _wait_for_rate_limit(self):
        """If MAX_SHIPS_PER_HOUR scrapes started in the last hour, waits for a slot and returns True."""
        now = time.monotonic()
        while self._recent_scrapes and now - self._recent_scrapes[0] >= 3600:
            self._recent_scrapes.popleft()
        if len(self._recent_scrapes) < self.settings.max_ships_per_hour:
            self._cap_logged = False
            return False
        delay = 3600 - (now - self._recent_scrapes[0])
        if not self._cap_logged:
            log.warning(
                "Reached %d ships/hour; pausing %.0fs. If this happens often, ships are falling "
                "behind schedule: raise MAX_SHIPS_PER_HOUR or lengthen the intervals",
                self.settings.max_ships_per_hour, delay,
            )
            self._cap_logged = True
        self._wait(delay)
        return True

    def _maybe_sync(self):
        if not self.settings.sync_my_ships:
            return
        if time.monotonic() - self._last_sync < self.settings.poll_seconds:
            return
        self._sync()

    def _sync(self):
        self._last_sync = time.monotonic()
        r = self.store.sync_ships(self.settings.my_ships_table)
        table = self.settings.my_ships_table
        if r["added"] is None:
            log.debug("Table %s not found; my-ship sync skipped", table)
        if r["added"]:
            log.info("Added %d new my-ship(s) from %s: %s", len(r["added"]), table, r["added"])
        if r["promoted"]:
            log.info("%d ship(s) became my-ships, refreshing now: %s", len(r["promoted"]), r["promoted"])
        if r["demoted"]:
            log.info("%d ship(s) are no longer my-ships", r["demoted"])
        if r["backfilled"]:
            log.info("Added %d ship(s) from mainshipdata to the watch list", len(r["backfilled"]))

    def _cool_down(self):
        self._cooldowns += 1
        delay = min(BLOCK_COOLDOWN_BASE * 2 ** (self._cooldowns - 1), BLOCK_COOLDOWN_MAX)
        log.error(
            "Blocked by MarineTraffic on %d ships in a row; pausing %d minutes",
            self._consecutive_blocks, delay // 60,
        )
        self._consecutive_blocks = 0
        if self._client is not None:
            self._client.close()
            self._client = None
        self._wait(delay)
