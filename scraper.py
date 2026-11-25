"""Fetches vessel data from MarineTraffic's JSON endpoints (the same ones the vessel page calls)."""
import random
import time

from curl_cffi import requests

BASE = "https://www.marinetraffic.com"
VESSEL_ENDPOINTS = ("general", "summary", "position", "voyage")
BLOCK_STATUSES = (403, 429, 503)
PORT_CACHE_TTL = 24 * 3600
PORT_CACHE_MAX = 5000


class ScrapeError(Exception):
    pass


class BlockedError(ScrapeError):
    """MarineTraffic refused every attempt with 403/429/503: we are blocked or rate limited."""


class MarineTrafficClient:
    def __init__(self, retries=3, backoff=5.0, timeout=30, proxy=None, sleep=time.sleep):
        self.retries = retries
        self.backoff = backoff
        self.timeout = timeout
        self.proxy = proxy
        self._sleep = sleep
        self._port_cache = {}
        self.session = None
        self._new_session()

    def _new_session(self):
        if self.session is not None:
            self.session.close()
        # Chrome TLS fingerprint impersonation is what gets us past Cloudflare.
        proxies = {"http": self.proxy, "https": self.proxy} if self.proxy else None
        self.session = requests.Session(impersonate="chrome", proxies=proxies)
        self._warmed = set()

    def close(self):
        if self.session is not None:
            self.session.close()
            self.session = None

    def _warm_up(self, ship_id):
        """Load the HTML page once so the session carries Cloudflare cookies, like a real visit."""
        if ship_id in self._warmed:
            return
        self.session.get(self._page_url(ship_id), timeout=self.timeout)
        self._warmed.add(ship_id)

    @staticmethod
    def _page_url(ship_id):
        return f"{BASE}/en/ais/details/ships/shipid:{ship_id}"

    def _get_json(self, path, ship_id):
        headers = {
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": self._page_url(ship_id),
        }
        last = None
        blocked = 0
        for attempt in range(1, self.retries + 1):
            try:
                self._warm_up(ship_id)
                r = self.session.get(BASE + path, headers=headers, timeout=self.timeout)
                if r.status_code == 200 and "json" in r.headers.get("content-type", ""):
                    try:
                        return r.json()
                    except ValueError:
                        last = "HTTP 200 with invalid JSON"
                elif r.status_code == 404:
                    raise ScrapeError(f"{path}: 404 not found (wrong ship id?)")
                else:
                    last = f"HTTP {r.status_code}"
                    if r.status_code in BLOCK_STATUSES:
                        blocked += 1
                        self._new_session()  # blocked or rate limited: start fresh
            except ScrapeError:
                raise
            except Exception as e:  # network errors
                last = repr(e)
            if attempt < self.retries:
                self._sleep(self.backoff * attempt + random.uniform(0, 2))
        if blocked == self.retries:
            raise BlockedError(f"{path}: blocked on all {self.retries} attempts ({last})")
        raise ScrapeError(f"{path}: failed after {self.retries} attempts ({last})")

    def get_port(self, port_id, ship_id):
        if port_id is None:
            return None
        cached = self._port_cache.get(port_id)
        if cached is not None and time.monotonic() - cached[0] < PORT_CACHE_TTL:
            return cached[1]
        port = self._get_json(f"/en/ports/{port_id}/general", ship_id)
        if len(self._port_cache) >= PORT_CACHE_MAX:
            self._port_cache.clear()
        self._port_cache[port_id] = (time.monotonic(), port)
        return port

    def fetch_vessel(self, ship_id):
        """Returns {'general':..., 'summary':..., 'position':..., 'voyage':..., 'ports': {...}}."""
        data = {}
        for ep in VESSEL_ENDPOINTS:
            data[ep] = self._get_json(f"/en/vessels/{ship_id}/{ep}", ship_id)
            self._sleep(random.uniform(0.5, 1.5))
        if not isinstance(data["general"], dict) or not data["general"]:
            raise ScrapeError(f"/en/vessels/{ship_id}/general: empty response")
        voyage = data["voyage"] if isinstance(data["voyage"], dict) else {}
        ports = {}
        for key in ("departurePortId", "arrivalPortId", "previousDeparturePortId"):
            pid = voyage.get(key)
            if pid is not None:
                try:
                    ports[pid] = self.get_port(pid, ship_id)
                except BlockedError:
                    raise
                except ScrapeError:
                    ports[pid] = None
        data["ports"] = ports
        return data
