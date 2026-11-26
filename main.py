"""MarineTraffic vessel tracker.

  python main.py migrate                      create / upgrade the database schema (run on each deploy)
  python main.py run                          the scheduled job: sync ships, scrape each once, exit
  python main.py run 314687 123456            scrape just these ships now
  python main.py status                       database check + summary of the last 24h of scrapes
  python main.py add 314687 [more ids...]     add ships to the watch list
  python main.py remove 314687                stop tracking a ship (history is kept)
  python main.py list                         show the watch list
  python main.py show                         print latest data for ALL tracked ships
  python main.py show 314687 123456           print latest data for specific ship IDs
  python main.py show --csv                   export latest data for all ships to CSV + mainshipdata table
  python main.py show 314687 --csv            export latest data for specific ships to CSV
  python main.py track 314687                 print position history (route) for one ship
  python main.py track 314687 --geojson       export route as GeoJSON LineString (route.geojson)
  python main.py export [out_dir]             export every table to CSV

Configuration is read from environment variables, or from the file named by
SHIPPING_ENV_FILE (default: ./.env); see .env.example.
"""
import argparse
import logging
import os
import signal
import sys
import threading

from dotenv import load_dotenv

load_dotenv(os.environ.get("SHIPPING_ENV_FILE") or None)

import psycopg2

from config import ConfigError, load_settings
from database import Database, Store
from logging_config import setup_logging
from cli import (
    cmd_add, cmd_remove, cmd_list, cmd_run, cmd_show, cmd_track, cmd_export,
    cmd_migrate, cmd_status,
)

log = logging.getLogger("tracker")


def build_parser():
    ap = argparse.ArgumentParser(description="MarineTraffic vessel tracker")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("migrate", help="create or upgrade the database schema")
    st = sub.add_parser("status", help="database check + summary of recent scrapes (read-only)")
    st.add_argument("--hours", type=int, default=24, help="summary window (default 24)")

    a = sub.add_parser("add", help="add ships to the watch list")
    a.add_argument("ship_ids", nargs="+", type=int)

    r = sub.add_parser("remove", help="stop tracking a ship (history is kept)")
    r.add_argument("ship_ids", nargs="+", type=int)

    sub.add_parser("list", help="show the watch list")

    run = sub.add_parser("run", help="sync the watch list and scrape every ship once (the scheduled job)")
    run.add_argument("ship_ids", nargs="*", type=int, help="only these ships (skips the sync)")

    s = sub.add_parser("show", help="print or export latest data")
    s.add_argument("ship_ids", nargs="*", type=int, help="ship IDs (omit for all tracked ships)")
    s.add_argument(
        "--csv", dest="csv_out", metavar="FILE", nargs="?", const="ships_latest.csv",
        help="write output to CSV instead of JSON (default file: ships_latest.csv)",
    )

    t = sub.add_parser("track", help="print or export position history (route) for one ship")
    t.add_argument("ship_id", type=int)
    t.add_argument("--since", type=int, metavar="EPOCH_TS", help="only positions at/after this unix timestamp")
    t.add_argument(
        "--geojson", metavar="FILE", nargs="?", const="route.geojson",
        help="write a GeoJSON LineString instead of JSON (default file: route.geojson)",
    )

    e = sub.add_parser("export", help="export every table to CSV files")
    e.add_argument("out_dir", nargs="?", default="export")

    return ap


COMMANDS = {
    "migrate":     cmd_migrate,
    "status":      cmd_status,
    "add":         cmd_add,
    "remove":      cmd_remove,
    "list":        cmd_list,
    "run":         cmd_run,
    "show":        cmd_show,
    "track":       cmd_track,
    "export":      cmd_export,
}

# Commands that should finish the current ship and stop cleanly on SIGTERM / Ctrl+C.
LONG_RUNNING = ("run",)


def main():
    args = build_parser().parse_args()

    try:
        settings = load_settings()
    except ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 78  # EX_CONFIG

    setup_logging(settings.log_level, settings.log_format)

    db = Database(settings)
    store = Store(db)
    try:
        if args.cmd in LONG_RUNNING:
            stop = threading.Event()

            def on_signal(signum, _frame):
                log.info("Received signal %s, finishing current ship and shutting down", signum)
                stop.set()

            signal.signal(signal.SIGINT, on_signal)
            signal.signal(signal.SIGTERM, on_signal)
            return COMMANDS[args.cmd](store, args, settings, stop=stop)
        return COMMANDS[args.cmd](store, args, settings)
    except psycopg2.OperationalError as e:
        log.error("Cannot reach the database: %s", e)
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
