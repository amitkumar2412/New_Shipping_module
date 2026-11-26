"""Shared fixtures.

Database tests run against a real Postgres named by TEST_DB_* env vars and are
skipped when it isn't reachable. Locally:

  docker run -d --rm --name shipping-test-pg -e POSTGRES_PASSWORD=test \
      -e POSTGRES_DB=shipping_test -p 55432:5432 postgres:17
  TEST_DB_PORT=55432 pytest
"""
import os
import sys
from dataclasses import replace

import psycopg2
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Keep a developer's .env from leaking into tests.
for _name in list(os.environ):
    if _name.startswith(("DB_", "SCRAPER_", "SHIPPING_")):
        del os.environ[_name]

from config import load_settings  # noqa: E402
from database import Database, Store  # noqa: E402
from database.store import EXPORTABLE_TABLES  # noqa: E402


@pytest.fixture
def settings():
    return replace(
        load_settings(),
        db_host=os.environ.get("TEST_DB_HOST", "localhost"),
        db_port=int(os.environ.get("TEST_DB_PORT", "55432")),
        db_name=os.environ.get("TEST_DB_NAME", "shipping_test"),
        db_user=os.environ.get("TEST_DB_USER", "postgres"),
        db_password=os.environ.get("TEST_DB_PASSWORD", "test"),
        db_sslmode="disable",
        db_connect_timeout=3,
    )


@pytest.fixture(scope="session")
def _db_available():
    s = load_settings()
    try:
        psycopg2.connect(
            host=os.environ.get("TEST_DB_HOST", "localhost"),
            port=int(os.environ.get("TEST_DB_PORT", "55432")),
            dbname=os.environ.get("TEST_DB_NAME", "shipping_test"),
            user=os.environ.get("TEST_DB_USER", "postgres"),
            password=os.environ.get("TEST_DB_PASSWORD", "test"),
            sslmode="disable",
            connect_timeout=3,
        ).close()
    except psycopg2.OperationalError as e:
        pytest.skip(f"test database not reachable: {e}")
    return s


@pytest.fixture
def store(settings, _db_available):
    db = Database(settings)
    s = Store(db)
    s.migrate()
    with db.transaction() as cur:
        cur.execute("TRUNCATE {} RESTART IDENTITY".format(", ".join(EXPORTABLE_TABLES + ("snapshots",))))
        cur.execute("DROP TABLE IF EXISTS shipwithid")
    yield s
    db.close()


@pytest.fixture
def shipwithid(store):
    """Creates chem-os's ship master table, shaped as chem-os's ShipMasterService uses it."""
    with store.db.transaction() as cur:
        cur.execute(
            "CREATE TABLE shipwithid (id BIGSERIAL PRIMARY KEY, ship_id BIGINT UNIQUE, "
            "ship_name TEXT, my_ship BOOLEAN DEFAULT FALSE)"
        )

    def add(ship_id, name, my_ship=True):
        with store.db.transaction() as cur:
            cur.execute(
                "INSERT INTO shipwithid (ship_id, ship_name, my_ship) VALUES (%s, %s, %s) "
                "ON CONFLICT (ship_id) DO UPDATE SET my_ship = EXCLUDED.my_ship",
                (ship_id, name, my_ship),
            )

    return add


def vessel_payload(ship_id, name="TEST VESSEL", position_ts=1_700_000_000):
    """A MarineTraffic scrape result shaped like MarineTrafficClient.fetch_vessel's return value."""
    return {
        "general": {"name": name, "aisName": name, "imo": 9000000 + ship_id % 100000, "mmsi": 400000000 + ship_id,
                    "type": "Tanker", "subtype": "Chemical Tanker", "countryCode": "IN"},
        "summary": {"position": "somewhere", "particulars": "a tanker"},
        "position": {"timestamp": position_ts, "lat": 19.0, "lon": 72.8, "speed": 10.5,
                     "navigationalStatus": "Under Way", "areaName": "Arabian Sea", "areaCode": "ARAB", "timezone": 5.5},
        "voyage": {"departurePortId": 1, "arrivalPortId": 2, "departureTimestamp": position_ts - 86400,
                   "eta": position_ts + 86400, "reportedDestination": "SINGAPORE", "progress": 40},
        "ports": {1: {"name": "MUMBAI", "unlocode": "INBOM", "country": {"name": "India", "code": "IN"}},
                  2: {"name": "SINGAPORE", "unlocode": "SGSIN", "country": {"name": "Singapore", "code": "SG"}}},
    }


class FakeClient:
    """Stands in for MarineTrafficClient. `behaviour` maps ship_id -> payload or exception."""

    def __init__(self, behaviour=None):
        self.behaviour = behaviour or {}
        self.calls = []
        self.closed = False

    def fetch_vessel(self, ship_id):
        self.calls.append(ship_id)
        outcome = self.behaviour.get(ship_id)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome or vessel_payload(ship_id)

    def close(self):
        self.closed = True
