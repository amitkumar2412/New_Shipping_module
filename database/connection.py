"""PostgreSQL connection pool and shared time utilities."""
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import psycopg2
import psycopg2.extras
import psycopg2.pool


class Database:
    """Thread-safe connection pool.

    Every unit of work runs in its own transaction via `transaction()`: commit on
    success, rollback on error. A connection that hit a connection-level error is
    discarded instead of being returned to the pool, so one dropped connection
    can't poison later requests.
    """

    def __init__(self, settings):
        self._settings = settings
        self._pool = None
        self._lock = threading.Lock()

    def _conn_kwargs(self):
        s = self._settings
        return dict(
            host=s.db_host,
            port=s.db_port,
            dbname=s.db_name,
            user=s.db_user,
            password=s.db_password,
            sslmode=s.db_sslmode,
            connect_timeout=s.db_connect_timeout,
            application_name="shipping-module",
            keepalives=1,
            keepalives_idle=30,
            keepalives_interval=10,
            keepalives_count=3,
        )

    def _get_pool(self):
        if self._pool is None:
            with self._lock:
                if self._pool is None:
                    self._pool = psycopg2.pool.ThreadedConnectionPool(
                        self._settings.db_pool_min, self._settings.db_pool_max, **self._conn_kwargs()
                    )
        return self._pool

    def connect_dedicated(self):
        """A connection outside the pool, for session-scoped state such as advisory locks."""
        conn = psycopg2.connect(**self._conn_kwargs())
        conn.autocommit = True
        return conn

    @contextmanager
    def transaction(self, dict_rows=False):
        pool = self._get_pool()
        conn = pool.getconn()
        broken = False
        try:
            factory = psycopg2.extras.RealDictCursor if dict_rows else None
            with conn.cursor(cursor_factory=factory) as cur:
                yield cur
            conn.commit()
        except (psycopg2.OperationalError, psycopg2.InterfaceError):
            broken = True
            raise
        except BaseException:
            try:
                conn.rollback()
            except (psycopg2.OperationalError, psycopg2.InterfaceError):
                broken = True
            raise
        finally:
            pool.putconn(conn, close=broken or bool(conn.closed))

    def ping(self):
        with self.transaction() as cur:
            cur.execute("SELECT 1")
            return cur.fetchone()[0] == 1

    def close(self):
        if self._pool is not None:
            self._pool.closeall()
            self._pool = None


def now_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ts_to_utc(ts):
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ts_to_local(ts, tz_hours):
    if ts is None or tz_hours is None:
        return None
    tz = timezone(timedelta(hours=tz_hours))
    return datetime.fromtimestamp(ts, tz).isoformat()


def b(v):
    """Convert truthy value to 1/0/None for PostgreSQL SMALLINT columns."""
    return None if v is None else int(bool(v))
