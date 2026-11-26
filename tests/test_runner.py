import threading
from dataclasses import replace

import pytest

import runner as runner_module
from conftest import FakeClient
from runner import Runner
from scraper import BlockedError, ScrapeError


@pytest.fixture(autouse=True)
def no_pauses(monkeypatch):
    monkeypatch.setattr(runner_module, "BETWEEN_SHIPS", (0, 0))


def make_runner(store, settings, client):
    return Runner(store, settings, stop=threading.Event(), client_factory=lambda _s: client)


def test_run_syncs_and_scrapes_my_ships_first(store, settings, shipwithid):
    store.upsert_mainshipdata({"vessel_ship_id": 10, "vessel_name": "FROM INVENTORY"})
    shipwithid(20, "MINE")
    client = FakeClient()
    result = make_runner(store, settings, client).run()
    assert client.calls == [20, 10]
    assert (result.total, result.ok, result.failed, result.success) == (2, 2, 0, True)
    assert store.latest(20)["vessel"]["name"] == "TEST VESSEL"
    assert client.closed


def test_second_run_soon_after_skips_scraped_ships(store, settings):
    store.add_ship(1)
    make_runner(store, settings, FakeClient()).run()
    client = FakeClient()
    assert make_runner(store, settings, client).run().total == 0
    assert client.calls == []


def test_failures_are_logged_and_run_continues(store, settings):
    for sid in (1, 2, 3):
        store.add_ship(sid)
    client = FakeClient({2: ScrapeError("404")})
    result = make_runner(store, settings, client).run()
    assert client.calls == [1, 2, 3]
    assert (result.ok, result.failed, result.failed_ships) == (2, 1, [2])
    assert result.stopped_early is None
    assert store.scrape_status(2)["last_attempt"]["ok"] == 0


def test_run_stops_after_consecutive_blocks(store, settings):
    for sid in (1, 2, 3, 4, 5):
        store.add_ship(sid)
    client = FakeClient({sid: BlockedError("403") for sid in (1, 2, 3, 4, 5)})
    result = make_runner(store, replace(settings, max_consecutive_blocks=3), client).run()
    assert client.calls == [1, 2, 3]
    assert "blocked" in result.stopped_early


def test_run_stops_at_max_duration(store, settings, monkeypatch):
    for sid in (1, 2, 3):
        store.add_ship(sid)
    clock = iter([0, 0, 11 * 60, 11 * 60, 11 * 60])
    monkeypatch.setattr(runner_module.time, "monotonic", lambda: next(clock, 11 * 60))
    client = FakeClient()
    result = make_runner(store, replace(settings, max_run_minutes=10), client).run()
    assert client.calls == [1]
    assert "MAX_RUN_MINUTES" in result.stopped_early


def test_run_refuses_while_another_run_holds_lock(store, settings):
    store.add_ship(1)
    held = store.try_run_lock()
    try:
        client = FakeClient()
        assert make_runner(store, settings, client).run() is None
        assert client.calls == []
    finally:
        held.close()


def test_shutdown_finishes_current_ship_then_stops(store, settings):
    for sid in (1, 2, 3):
        store.add_ship(sid)
    r = None

    class StopAfterFirst(FakeClient):
        def fetch_vessel(self, ship_id):
            data = super().fetch_vessel(ship_id)
            r.stop.set()
            return data

    client = StopAfterFirst()
    r = make_runner(store, settings, client)
    result = r.run()
    assert client.calls == [1]
    assert result.ok == 1 and result.stopped_early == "shutdown requested"


def test_manual_run_for_specific_ships(store, settings, shipwithid):
    shipwithid(20, "MINE")
    client = FakeClient()
    result = make_runner(store, settings, client).run(ship_ids=[99])
    assert client.calls == [99]
    assert result.ok == 1
    assert store.active_ships() == []  # manual run doesn't sync the watch list
