from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from api import create_app
from conftest import vessel_payload


@pytest.fixture
def client_for(store, settings):
    def factory(api_key=None):
        app = create_app(replace(settings, api_key=api_key), store=store)
        return TestClient(app, raise_server_exceptions=False)
    return factory


def test_probes_need_no_key(client_for):
    c = client_for("secret")
    with c:
        assert c.get("/health").json() == {"status": "ok"}
        assert c.get("/ready").status_code == 200


def test_read_endpoints_require_key_when_configured(client_for, store):
    store.add_ship(1)
    store.save(1, vessel_payload(1, "ALPHA"))
    with client_for("secret") as c:
        assert c.get("/ship-inventory").status_code == 401
        assert c.get("/ship-inventory", headers={"X-API-Key": "wrong"}).status_code == 401
        r = c.get("/ship-inventory", headers={"X-API-Key": "secret"})
        assert r.status_code == 200
        assert r.json()["data"][0]["vessel_name"] == "ALPHA"


def test_read_endpoints_open_without_key(client_for):
    with client_for(None) as c:
        assert c.get("/mainshipdata").status_code == 200


def test_scrape_trigger_requires_configured_key(client_for):
    with client_for(None) as c:
        assert c.post("/ships/5/scrape").status_code == 503


def test_scrape_trigger_queues_ship(client_for, store):
    with client_for("secret") as c:
        assert c.post("/ships/5/scrape").status_code == 401
        r = c.post("/ships/5/scrape", headers={"X-API-Key": "secret"})
        assert r.status_code == 202
        assert r.json() == {"ship_id": 5, "status": "queued"}
        status = c.get("/ships/5/status", headers={"X-API-Key": "secret"}).json()
        assert status["scrape_requested_at"] is not None
        assert status["last_attempt"] is None
    assert store.due_ships(360, 1440, 600) == [5]


@pytest.mark.parametrize("ship_id", ["0", "-1", "abc", "99999999999"])
def test_scrape_trigger_validates_ship_id(client_for, ship_id):
    with client_for("secret") as c:
        assert c.post(f"/ships/{ship_id}/scrape", headers={"X-API-Key": "secret"}).status_code == 422


def test_status_unknown_ship(client_for):
    with client_for(None) as c:
        assert c.get("/ships/77/status").status_code == 404


def test_pagination_limits(client_for):
    with client_for(None) as c:
        assert c.get("/mainshipdata?limit=11").status_code == 422
        assert c.get("/ship-inventory/search?q=").status_code == 422


def test_internal_errors_are_not_leaked(client_for, store, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("password=hunter2")
    monkeypatch.setattr(store, "get_all_mainshipdata", boom)
    with client_for(None) as c:
        r = c.get("/mainshipdata")
    assert r.status_code == 500
    assert "hunter2" not in r.text
