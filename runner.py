"""One scheduled scrape run: sync the watch list, scrape every ship once, exit.

Started by the systemd timer twice a day (~09:00 and ~17:00 IST). A run:
  1. takes the run lock, so a manual run and a timer run never overlap,
  2. syncs the watch list: ships chem-os flags `my_ship` (in shipwithid) and
     every ship in mainshipdata,
  3. scrapes each active ship once — my-ships first — with a 3-8s pause between
     ships, skipping ships already scraped in the last SKIP_RECENT_HOURS,
  4. stops early if MarineTraffic blocks MAX_CONSECUTIVE_BLOCKS ships in a row,
     if MAX_RUN_MINUTES is reached, or on SIGTERM (after the current ship).

Every attempt is recorded in scrape_log; `python main.py status` summarises them.
"""
import logging
import random
import threading
import time
from dataclasses import dataclass, field

import psycopg2

from scraper import BlockedError, MarineTrafficClient, ScrapeError

log = logging.getLogger("tracker.run")

BETWEEN_SHIPS = (3, 8)


def make_client(settings):
    return MarineTrafficClient(timeout=settings.scraper_timeout, proxy=settings.scraper_proxy)


@dataclass
class RunResult:
    total: int = 0
    ok: int = 0
    failed: int = 0
    stopped_early: str | None = None
    failed_ships: list = field(default_factory=list)

    @property
    def success(self):
        return self.stopped_early is None and self.failed == 0


class Runner:
    def __init__(self, store, settings, stop=None, client_factory=make_client):
        self.store = store
        self.settings = settings
        self.stop = stop or threading.Event()
        self._client_factory = client_factory
        self._client = None
        self._consecutive_blocks = 0

    def run(self, ship_ids=None):
        """Runs once. Returns a RunResult, or None if another run holds the lock.

        `ship_ids` limits the run to those ships (for manual runs); otherwise the
        watch list is synced and every ship due in this run is scraped.
        """
        lock = self.store.try_run_lock()
        if lock is None:
            return None
        started = time.monotonic()
        result = RunResult()
        try:
            if ship_ids is None:
                if self.settings.sync_my_ships:
                    self._sync()
                ship_ids = self.store.ships_to_scrape(self.settings.skip_recent_hours)
            result.total = len(ship_ids)
            log.info("Run starting: %d ship(s) to scrape", result.total)

            deadline = started + self.settings.max_run_minutes * 60
            for i, ship_id in enumerate(ship_ids):
                if self.stop.is_set():
                    result.stopped_early = "shutdown requested"
                    break
                if time.monotonic() > deadline:
                    result.stopped_early = f"reached MAX_RUN_MINUTES ({self.settings.max_run_minutes})"
                    break
                if self.scrape_one(ship_id):
                    result.ok += 1
                else:
                    result.failed += 1
                    result.failed_ships.append(ship_id)
                if self._consecutive_blocks >= self.settings.max_consecutive_blocks:
                    result.stopped_early = (
                        f"blocked by MarineTraffic on {self._consecutive_blocks} ships in a row"
                    )
                    break
                if i < len(ship_ids) - 1:
                    self.stop.wait(random.uniform(*BETWEEN_SHIPS))
        finally:
            if self._client is not None:
                self._client.close()
                self._client = None
            lock.close()

        minutes = (time.monotonic() - started) / 60
        skipped = result.total - result.ok - result.failed
        if result.stopped_early:
            log.error(
                "Run stopped early (%s): %d ok, %d failed, %d not attempted, %.1f min",
                result.stopped_early, result.ok, result.failed, skipped, minutes,
            )
        else:
            log.info("Run finished: %d/%d ok, %.1f min", result.ok, result.total, minutes)
        if result.failed_ships:
            log.warning("Failed ships: %s", result.failed_ships)
        return result

    def scrape_one(self, ship_id):
        """Scrapes and stores one ship. Returns True on success; failures go to scrape_log."""
        if self._client is None:
            self._client = self._client_factory(self.settings)
        started = time.monotonic()
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
        p = data["position"] or {}
        log.info(
            "%s %s | %s | lat=%s lon=%s spd=%s | %s | %.1fs",
            ship_id, data["general"].get("name"),
            p.get("navigationalStatus"), p.get("lat"), p.get("lon"), p.get("speed"),
            "NEW position" if new_pos else "position unchanged", time.monotonic() - started,
        )
        return True

    def _sync(self):
        r = self.store.sync_ships(self.settings.my_ships_table)
        table = self.settings.my_ships_table
        if r["added"] is None:
            log.warning("Table %s not found; only mainshipdata ships are tracked", table)
        if r["added"]:
            log.info("Added %d new my-ship(s) from %s: %s", len(r["added"]), table, r["added"])
        if r["promoted"]:
            log.info("%d tracked ship(s) became my-ships: %s", len(r["promoted"]), r["promoted"])
        if r["demoted"]:
            log.info("%d ship(s) are no longer my-ships", r["demoted"])
        if r["backfilled"]:
            log.info("Added %d ship(s) from mainshipdata to the watch list", len(r["backfilled"]))
