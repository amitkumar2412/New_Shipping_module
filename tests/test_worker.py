import threading
import time
from dataclasses import replace

import worker as worker_module
from conftest import FakeClient
from scraper import BlockedError, ScrapeError
from worker import Worker, heartbeat_age


def make_worker(store, settings, client):
    return Worker(store, settings, stop=threading.Event(), client_factory=lambda _s: client)


def no_wait(monkeypatch):
    monkeypatch.setattr(worker_module, "BETWEEN_SHIPS", (0, 0))


def test_run_once_syncs_my_ships_and_scrapes(store, settings, shipwithid, monkeypatch):
    no_wait(monkeypatch)
    shipwithid(100, "MINE")
    store.add_ship(200)
    client = FakeClient({200: ScrapeError("404")})
    assert make_worker(store, settings, client).run_once() == (1, 2)
    assert sorted(client.calls) == [100, 200]
    assert store.latest(100)["vessel"]["name"] == "TEST VESSEL"
    assert store.scrape_status(200)["last_attempt"]["ok"] == 0
    assert client.closed


def test_run_once_refuses_when_another_worker_holds_lock(store, settings):
    held = store.try_worker_lock()
    try:
        assert make_worker(store, settings, FakeClient()).run_once() is None
    finally:
        held.close()


def test_run_once_aborts_after_consecutive_blocks(store, settings, monkeypatch):
    no_wait(monkeypatch)
    for sid in (1, 2, 3, 4, 5):
        store.add_ship(sid)
    client = FakeClient({sid: BlockedError("403") for sid in (1, 2, 3, 4, 5)})
    ok, total = make_worker(store, replace(settings, max_consecutive_blocks=3), client).run_once()
    assert ok == 0 and total == 5
    assert client.calls == [1, 2, 3]


def test_worker_scrapes_new_my_ship_promptly(store, settings, shipwithid, monkeypatch):
    """The end-to-end flow: chem-os flags a ship, the running worker picks it up."""
    no_wait(monkeypatch)
    client = FakeClient()
    # Config enforces >= 10s; a 1s poll keeps the test fast.
    w = make_worker(store, replace(settings, poll_seconds=1), client)
    t = threading.Thread(target=w.run_forever, daemon=True)
    t.start()
    try:
        time.sleep(1.5)  # worker is now idle: nothing to scrape
        assert client.calls == []
        shipwithid(4242, "NEW MY SHIP")
        deadline = time.time() + 10
        while time.time() < deadline and store.latest(4242)["vessel"] is None:
            time.sleep(0.2)
        assert store.latest(4242)["vessel"]["name"] == "TEST VESSEL"
    finally:
        w.stop.set()
        t.join(10)
    assert not t.is_alive()
    assert heartbeat_age(settings.heartbeat_file) < 30


def test_worker_cools_down_when_blocked(store, settings, monkeypatch):
    no_wait(monkeypatch)
    for sid in (1, 2, 3):
        store.add_ship(sid)
    client = FakeClient({sid: BlockedError("403") for sid in (1, 2, 3)})
    w = make_worker(store, replace(settings, max_consecutive_blocks=3), client)
    waits = []

    def fake_wait(seconds):
        waits.append(seconds)
        if seconds >= worker_module.BLOCK_COOLDOWN_BASE:
            w.stop.set()

    monkeypatch.setattr(w, "_wait", fake_wait)
    w.run_forever()
    assert client.calls == [1, 2, 3]
    assert worker_module.BLOCK_COOLDOWN_BASE in waits


def test_standby_worker_does_not_scrape(store, settings):
    store.add_ship(1)
    held = store.try_worker_lock()
    client = FakeClient()
    w = make_worker(store, settings, client)
    try:
        t = threading.Thread(target=w.run_forever, daemon=True)
        t.start()
        time.sleep(1)
        w.stop.set()
        t.join(10)
    finally:
        held.close()
    assert client.calls == []


def test_hourly_cap_limits_scrapes(store, settings, monkeypatch):
    no_wait(monkeypatch)
    for sid in (1, 2, 3, 4):
        store.add_ship(sid)
    client = FakeClient()
    w = make_worker(store, replace(settings, max_ships_per_hour=2), client)
    waits = []

    def fake_wait(seconds):
        waits.append(seconds)
        if seconds > 3000:  # the cap kicked in: stop instead of waiting an hour
            w.stop.set()

    monkeypatch.setattr(w, "_wait", fake_wait)
    w.run_forever()
    assert client.calls == [1, 2]
    assert any(s > 3000 for s in waits)
