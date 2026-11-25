import pytest

import scraper
from scraper import BlockedError, MarineTrafficClient, ScrapeError


class FakeResponse:
    def __init__(self, status, body=None, content_type="application/json"):
        self.status_code = status
        self._body = body
        self.headers = {"content-type": content_type}

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class FakeSession:
    """Returns queued responses for API paths; HTML warm-up requests always succeed."""

    instances = []

    def __init__(self, routes):
        self.routes = routes
        self.closed = False
        FakeSession.instances.append(self)

    def get(self, url, headers=None, timeout=None):
        if "/ais/details/" in url:
            return FakeResponse(200, content_type="text/html")
        path = url.replace(scraper.BASE, "")
        queue = self.routes[path]
        return queue.pop(0) if len(queue) > 1 else queue[0]

    def close(self):
        self.closed = True


@pytest.fixture
def make_client(monkeypatch):
    def factory(routes):
        FakeSession.instances = []
        monkeypatch.setattr(scraper.requests, "Session", lambda **kw: FakeSession(routes))
        return MarineTrafficClient(retries=3, backoff=0, sleep=lambda s: None)
    return factory


def vessel_routes(ship_id, voyage=None):
    base = f"/en/vessels/{ship_id}"
    return {
        f"{base}/general": [FakeResponse(200, {"name": "ALPHA"})],
        f"{base}/summary": [FakeResponse(200, {})],
        f"{base}/position": [FakeResponse(200, {"lat": 1})],
        f"{base}/voyage": [FakeResponse(200, voyage or {})],
    }


def test_fetch_vessel_with_ports(make_client):
    routes = vessel_routes(7, {"departurePortId": 10, "arrivalPortId": 11})
    routes["/en/ports/10/general"] = [FakeResponse(200, {"name": "A"})]
    routes["/en/ports/11/general"] = [FakeResponse(404)]
    data = make_client(routes).fetch_vessel(7)
    assert data["general"]["name"] == "ALPHA"
    assert data["ports"] == {10: {"name": "A"}, 11: None}


def test_404_is_not_retried(make_client):
    routes = vessel_routes(7)
    routes["/en/vessels/7/general"] = [FakeResponse(404)]
    with pytest.raises(ScrapeError, match="404"):
        make_client(routes).fetch_vessel(7)


def test_transient_error_then_success(make_client):
    routes = vessel_routes(7)
    routes["/en/vessels/7/general"] = [FakeResponse(500), FakeResponse(200, {"name": "ALPHA"})]
    assert make_client(routes).fetch_vessel(7)["general"]["name"] == "ALPHA"


def test_blocked_on_every_attempt_raises_blocked_error(make_client):
    routes = vessel_routes(7)
    routes["/en/vessels/7/general"] = [FakeResponse(403)]
    client = make_client(routes)
    with pytest.raises(BlockedError):
        client.fetch_vessel(7)
    # Each block discards the session and starts a fresh one.
    assert len(FakeSession.instances) == 4
    assert all(s.closed for s in FakeSession.instances[:-1])


def test_mixed_failures_are_plain_scrape_errors(make_client):
    routes = vessel_routes(7)
    routes["/en/vessels/7/general"] = [FakeResponse(403), FakeResponse(500), FakeResponse(500)]
    with pytest.raises(ScrapeError) as exc:
        make_client(routes).fetch_vessel(7)
    assert not isinstance(exc.value, BlockedError)


def test_invalid_json_is_retried(make_client):
    routes = vessel_routes(7)
    routes["/en/vessels/7/general"] = [FakeResponse(200, ValueError("bad")), FakeResponse(200, {"name": "ALPHA"})]
    assert make_client(routes).fetch_vessel(7)["general"]["name"] == "ALPHA"


def test_empty_general_is_an_error(make_client):
    routes = vessel_routes(7)
    routes["/en/vessels/7/general"] = [FakeResponse(200, {})]
    with pytest.raises(ScrapeError, match="empty"):
        make_client(routes).fetch_vessel(7)


def test_port_cache(make_client):
    routes = vessel_routes(7)
    routes["/en/ports/10/general"] = [FakeResponse(200, {"name": "A"}), FakeResponse(500)]
    client = make_client(routes)
    assert client.get_port(10, 7) == {"name": "A"}
    assert client.get_port(10, 7) == {"name": "A"}  # second call would get 500 if not cached
