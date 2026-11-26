import pytest

from conftest import vessel_payload


def age(store, ship_id, minutes):
    """Pretends the ship's scrape attempts happened `minutes` ago."""
    with store.db.transaction() as cur:
        cur.execute(
            "UPDATE scrape_log SET scraped_at = to_char(now() at time zone 'utc' - make_interval(mins => %s), "
            "'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"') WHERE ship_id = %s",
            (minutes, ship_id),
        )


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


def test_position_history_builds_a_route(store):
    store.add_ship(1)
    for ts in (1_700_000_000, 1_700_028_800, 1_700_057_600):  # three scrapes, 8h apart
        store.save(1, vessel_payload(1, position_ts=ts))
    assert [p["position_ts"] for p in store.track(1)] == [1_700_000_000, 1_700_028_800, 1_700_057_600]


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


def test_sync_adds_my_ships(store, shipwithid):
    shipwithid(100, "MINE", my_ship=True)
    shipwithid(200, "NOT MINE", my_ship=False)
    assert store.sync_ships("shipwithid")["added"] == [100]
    assert store.sync_ships("shipwithid")["added"] == []  # already on the watch list
    assert store.active_ships() == [100]
    assert store.scrape_status(100)["my_ship"] == 1


def test_sync_promotes_known_ship(store, shipwithid):
    """A ship already tracked as an 'other' ship gets picked for a purchase."""
    store.add_ship(300)
    shipwithid(300, "NOW MINE")
    assert store.sync_ships("shipwithid")["promoted"] == [300]
    assert store.scrape_status(300)["my_ship"] == 1


def test_sync_demotes_unflagged_ship(store, shipwithid):
    shipwithid(100, "MINE")
    store.sync_ships("shipwithid")
    shipwithid(100, "MINE", my_ship=False)
    assert store.sync_ships("shipwithid")["demoted"] == 1
    assert store.scrape_status(100)["my_ship"] == 0
    assert store.active_ships() == [100]  # still tracked


def test_sync_backfills_ships_from_mainshipdata(store):
    store.upsert_mainshipdata({"vessel_ship_id": 555, "vessel_name": "OLD ENTRY"})
    r = store.sync_ships("shipwithid")
    assert r["added"] is None  # no chem-os table in this database
    assert r["backfilled"] == [555]
    assert store.scrape_status(555)["my_ship"] == 0


def test_sync_respects_manual_removal(store, shipwithid):
    shipwithid(100, "MINE")
    store.sync_ships("shipwithid")
    store.remove_ship(100)
    store.sync_ships("shipwithid")
    assert store.active_ships() == []


def test_sync_ignores_ids_out_of_range(store, shipwithid):
    shipwithid(5_000_000_000, "TOO BIG")
    assert store.sync_ships("shipwithid")["added"] == []


def test_ships_to_scrape_order_and_skip(store, shipwithid):
    shipwithid(4, "MINE")
    store.sync_ships("shipwithid")
    for sid in (1, 2, 3):
        store.add_ship(sid)
    store.save(1, vessel_payload(1))
    age(store, 1, 10 * 60)            # scraped 10h ago
    store.save(2, vessel_payload(2))  # scraped just now: skipped
    store.log_error(3, "boom")        # failed just now: retried (only successes are skipped)
    store.add_ship(5)
    store.remove_ship(5)              # inactive: never scraped
    # my-ship first, then never scraped successfully, then oldest success
    assert store.ships_to_scrape(skip_recent_hours=2) == [4, 3, 1]
    assert store.ships_to_scrape(skip_recent_hours=0) == [4, 3, 1, 2]


def test_run_summary(store):
    store.add_ship(1)
    store.add_ship(2)
    store.save(1, vessel_payload(1))
    store.log_error(2, "HTTP 500")
    s = store.run_summary(24)
    assert (s["attempts"], s["ok"], s["ships"], s["tracked"]) == (2, 1, 2, 2)
    assert [f["ship_id"] for f in s["failures"]] == [2]


def test_run_lock_is_exclusive(store):
    first = store.try_run_lock()
    assert first is not None
    assert store.try_run_lock() is None
    first.close()
    second = store.try_run_lock()
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
