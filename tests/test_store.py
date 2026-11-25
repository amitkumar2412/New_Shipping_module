import pytest

from conftest import vessel_payload


def test_migrate_is_idempotent(store):
    store.migrate()
    store.migrate()
    assert store.schema_ready()


def test_save_writes_tables_and_mainshipdata_atomically(store):
    store.add_ship(314687)
    assert store.save(314687, vessel_payload(314687, "ALPHA")) is True
    # Same AIS timestamp again: no new position.
    assert store.save(314687, vessel_payload(314687, "ALPHA")) is False

    latest = store.latest(314687)
    assert latest["vessel"]["name"] == "ALPHA"
    assert latest["voyage"]["departure_port_name"] == "MUMBAI"

    rows, total = store.get_all_mainshipdata()
    assert total == 1
    assert rows[0]["vessel_ship_id"] == 314687
    assert rows[0]["voyage_arrival_port_name"] == "SINGAPORE"
    assert rows[0]["position_area_name"] == "Arabian Sea"


def test_failed_save_rolls_back_everything(store):
    store.add_ship(1)
    bad = vessel_payload(1)
    bad["position"]["timestamp"] = "not-a-number"  # BIGINT column rejects this mid-transaction
    with pytest.raises(Exception):
        store.save(1, bad)
    assert store.latest(1)["vessel"] is None
    assert store.get_all_mainshipdata()[1] == 0
    # The pool hands out a healthy connection afterwards.
    assert store.db.ping()


MY, OTHER, STATIONARY = 360, 1440, 600


def due(store, **kw):
    return store.due_ships(MY, OTHER, STATIONARY, **kw)


def age(store, ship_id, minutes):
    """Pretends every record we have for the ship is `minutes` old."""
    with store.db.transaction() as cur:
        ts = f"to_char(now() at time zone 'utc' - interval '{int(minutes)} minutes', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"')"
        cur.execute(f"UPDATE scrape_log SET scraped_at = {ts} WHERE ship_id = %s", (ship_id,))
        cur.execute(f"UPDATE vessels SET updated_at = {ts} WHERE ship_id = %s", (ship_id,))
        cur.execute(f"UPDATE mainshipdata SET synced_at = {ts} WHERE vessel_ship_id = %s", (ship_id,))


def set_status(store, ship_id, status, speed):
    with store.db.transaction() as cur:
        cur.execute(
            "UPDATE mainshipdata SET position_navigational_status = %s, position_speed = %s WHERE vessel_ship_id = %s",
            (status, speed, ship_id),
        )


def test_sync_adds_my_ships(store, shipwithid):
    shipwithid(100, "MINE", my_ship=True)
    shipwithid(200, "NOT MINE", my_ship=False)
    r = store.sync_ships("shipwithid")
    assert r["added"] == [100]
    assert store.sync_ships("shipwithid")["added"] == []  # already on the watch list
    assert store.active_ships() == [100]
    assert store.scrape_status(100)["my_ship"] == 1


def test_sync_promotes_known_ship_and_queues_refresh(store, shipwithid):
    """A ship already tracked as an 'other' ship gets picked for a purchase."""
    store.add_ship(300)
    store.save(300, vessel_payload(300))
    assert due(store) == []
    shipwithid(300, "NOW MINE")
    r = store.sync_ships("shipwithid")
    assert r["promoted"] == [300]
    status = store.scrape_status(300)
    assert status["my_ship"] == 1 and status["scrape_requested_at"] is not None
    assert due(store) == [300]


def test_sync_demotes_unflagged_ship(store, shipwithid):
    shipwithid(100, "MINE")
    store.sync_ships("shipwithid")
    shipwithid(100, "MINE", my_ship=False)
    assert store.sync_ships("shipwithid")["demoted"] == 1
    assert store.scrape_status(100)["my_ship"] == 0
    assert store.active_ships() == [100]  # still tracked, just less often


def test_sync_backfills_ships_from_mainshipdata(store):
    store.upsert_mainshipdata({"vessel_ship_id": 555, "vessel_name": "OLD ENTRY"})
    r = store.sync_ships("shipwithid")
    assert r["added"] is None  # no chem-os table in this database
    assert r["backfilled"] == [555]
    assert store.scrape_status(555)["my_ship"] == 0
    # It has data from before, so it isn't scraped immediately.
    assert due(store) == []


def test_sync_respects_manual_removal(store, shipwithid):
    shipwithid(100, "MINE")
    store.sync_ships("shipwithid")
    store.remove_ship(100)
    store.sync_ships("shipwithid")
    assert store.active_ships() == []


def test_sync_ignores_ids_out_of_range(store, shipwithid):
    shipwithid(5_000_000_000, "TOO BIG")
    assert store.sync_ships("shipwithid")["added"] == []


def test_due_ships_priority(store):
    for sid in (1, 2, 3, 4):
        store.add_ship(sid)
    store.save(1, vessel_payload(1))          # fresh: not due
    store.save(2, vessel_payload(2))
    store.request_scrape(2)                   # fresh but explicitly requested
    store.log_error(3, "boom")                # failed recently: not due
    # 4 was never attempted
    assert due(store) == [2, 4]
    assert due(store, limit=1) == [2]


def test_my_ships_refresh_every_6h_others_every_24h(store, shipwithid):
    shipwithid(1, "MINE")
    store.sync_ships("shipwithid")
    store.add_ship(2)
    for sid in (1, 2):
        store.save(sid, vessel_payload(sid))
        age(store, sid, 7 * 60)
    assert due(store) == [1]
    age(store, 2, 27 * 60)  # 3h overdue vs. 1h for ship 1
    assert due(store) == [2, 1]  # most overdue first


def test_stationary_my_ship_waits_10h(store, shipwithid):
    shipwithid(1, "MINE")
    store.sync_ships("shipwithid")
    store.save(1, vessel_payload(1))
    set_status(store, 1, "Moored", 0.0)
    age(store, 1, 7 * 60)
    assert due(store) == []
    age(store, 1, 11 * 60)
    assert due(store) == [1]


def test_stationary_detection(store, shipwithid):
    shipwithid(1, "A")
    shipwithid(2, "B")
    shipwithid(3, "C")
    store.sync_ships("shipwithid")
    for sid in (1, 2, 3):
        store.save(sid, vessel_payload(sid))
        age(store, sid, 7 * 60)
    set_status(store, 1, "At Anchor", 0.1)
    set_status(store, 2, "Underway using Engine", 0.2)  # drifting: treated as stationary
    set_status(store, 3, "Underway using Engine", 12.8)
    assert due(store) == [3]


def test_stationary_never_shortens_the_24h_interval(store):
    store.add_ship(1)
    store.save(1, vessel_payload(1))
    set_status(store, 1, "Moored", 0.0)
    age(store, 1, 11 * 60)
    assert due(store) == []


def test_request_scrape_reactivates_and_clears_after_attempt(store):
    store.add_ship(9)
    store.remove_ship(9)
    store.request_scrape(9)
    status = store.scrape_status(9)
    assert status["active"] == 1 and status["scrape_requested_at"] is not None
    store.log_error(9, "boom")
    status = store.scrape_status(9)
    assert status["scrape_requested_at"] is None
    assert status["last_attempt"]["ok"] == 0


def test_worker_lock_is_exclusive(store):
    first = store.try_worker_lock()
    assert first is not None
    assert store.try_worker_lock() is None
    first.close()
    second = store.try_worker_lock()
    assert second is not None
    second.close()


def test_export_rejects_unknown_tables(store):
    with pytest.raises(ValueError):
        store.export_table("pg_authid")
    cols, _rows = store.export_table("ships")
    assert "ship_id" in cols


def test_search(store):
    store.add_ship(1)
    store.save(1, vessel_payload(1, "EVERGREEN STAR"))
    rows, total = store.search_ship_inventory("Evergren")
    assert total == 1 and rows[0]["ship_id"] == 1
