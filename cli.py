"""CLI command implementations — one function per subcommand.

Each returns a process exit code (0 = success).
"""
import csv
import json
import logging
import os

from database import Store, flatten
from database.store import EXPORTABLE_TABLES
from scraper import ScrapeError
from runner import Runner, make_client

log = logging.getLogger("tracker")


def cmd_migrate(store: Store, _args, _settings):
    store.migrate()
    print("Schema is up to date")
    return 0


def cmd_run(store: Store, args, settings, stop=None):
    """The scheduled job. Exit 0 = run completed; 2 = stopped early or every ship failed."""
    _require_schema(store)
    result = Runner(store, settings, stop=stop).run(ship_ids=args.ship_ids or None)
    if result is None:
        log.warning("Another run is already in progress; skipping this one")
        return 0
    if result.stopped_early or (result.total and result.ok == 0):
        return 2
    return 0


def cmd_status(store: Store, args, settings):
    """Read-only: database and schema check plus a summary of recent scrape runs."""
    store.db.ping()
    print("database:    reachable")
    if not store.schema_ready():
        print("schema:      MISSING or out of date - run: python main.py migrate")
        return 1
    print("schema:      up to date")
    found = store.table_exists(settings.my_ships_table)
    print(f"my-ships:    table {settings.my_ships_table} {'found' if found else 'NOT FOUND (my-ships will not be tracked)'}")
    s = store.run_summary(args.hours)
    print(f"tracked:     {s['tracked']} ships ({s['tracked_my_ships']} my-ships)")
    print(f"last {args.hours}h:    {s['attempts']} attempts, {s['ok']} ok, {s['ships']} ships, "
          f"last attempt {s['last_attempt'] or 'never'}")
    for f in s["failures"]:
        print(f"  failed {f['ship_id']} at {f['scraped_at']}: {(f['error'] or '')[:150]}")
    return 0


def cmd_add(store: Store, args, _settings):
    for sid in args.ship_ids:
        store.add_ship(sid)
    print("Tracking:", store.active_ships())
    return 0


def cmd_remove(store: Store, args, _settings):
    for sid in args.ship_ids:
        store.remove_ship(sid)
    print("Tracking:", store.active_ships())
    return 0


def cmd_list(store: Store, _args, _settings):
    for row in store.list_all_ships():
        print(row)
    return 0


def cmd_show(store: Store, args, settings):
    ship_ids = args.ship_ids if args.ship_ids else store.active_ships()
    if not ship_ids:
        log.warning("No ships tracked yet. Add ships with: python main.py add <ship_id>")

    results = []
    client = None
    try:
        for sid in ship_ids:
            result = store.latest(sid)
            if result["vessel"] is None:
                log.info("No stored data for %d — fetching live from MarineTraffic…", sid)
                if client is None:
                    client = make_client(settings)
                try:
                    data = client.fetch_vessel(sid)
                    store.add_ship(sid)
                    store.save(sid, data)
                    result = store.latest(sid)
                except ScrapeError as e:
                    log.error("Failed to fetch ship %d: %s", sid, e)
            results.append(result)
    finally:
        if client is not None:
            client.close()

    if args.csv_out:
        _write_csv(store, results, args.csv_out)
    else:
        for r in results:
            print(json.dumps(r, indent=2, ensure_ascii=False, default=str))
    return 0


def _write_csv(store: Store, results: list, csv_path: str):
    rows = [flatten(r) for r in results if r["vessel"] is not None]
    if not rows:
        print("No data to write.")
        return

    fieldnames = list(rows[0].keys())
    try:
        with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            w.writerows(rows)
        print(f"Wrote {len(rows)} ship(s) to {csv_path}")
    except PermissionError:
        log.warning("Cannot write %s — file may be open in another program. Skipping CSV.", csv_path)

    for row in rows:
        store.upsert_mainshipdata(row)
    print(f"Synced {len(rows)} ship(s) to mainshipdata table")


def cmd_track(store: Store, args, _settings):
    points = store.track(args.ship_id, since=args.since)
    if not points:
        print(f"No position history for {args.ship_id}")
        return 0

    if args.geojson:
        feature = {
            "type": "Feature",
            "properties": {"ship_id": args.ship_id},
            "geometry": {
                "type": "LineString",
                "coordinates": [
                    [p["lon"], p["lat"]] for p in points
                    if p["lon"] is not None and p["lat"] is not None
                ],
            },
        }
        with open(args.geojson, "w", encoding="utf-8") as f:
            json.dump(feature, f, ensure_ascii=False)
        print(f"Wrote {len(points)} point(s) to {args.geojson}")
    else:
        print(json.dumps(points, indent=2, ensure_ascii=False, default=str))
    return 0


def cmd_export(store: Store, args, _settings):
    os.makedirs(args.out_dir, exist_ok=True)
    for table in EXPORTABLE_TABLES:
        cols, rows = store.export_table(table)
        path = os.path.join(args.out_dir, f"{table}.csv")
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(cols)
            w.writerows(rows)
        print("wrote", path)
    return 0


def _require_schema(store: Store):
    if not store.schema_ready():
        raise SystemExit("Database schema is missing or out of date. Run: python main.py migrate")
