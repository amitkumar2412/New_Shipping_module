"""FastAPI REST interface for the shipping scraper database.

  uvicorn api:app --host 0.0.0.0 --port 8000

Every endpoint except /health and /ready requires the `X-API-Key` header when
API_KEY is set. POST /ships/{ship_id}/scrape always requires it.
"""
import logging
import secrets
from contextlib import asynccontextmanager

from dotenv import load_dotenv

load_dotenv()

import psycopg2
from fastapi import Depends, FastAPI, HTTPException, Path, Query, Request, status
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader

from config import load_settings
from database import Database, Store
from database.store import MAX_SHIP_ID
from logging_config import setup_logging

log = logging.getLogger("tracker.api")

MAX_PAGE_SIZE = 10

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def create_app(settings=None, store=None):
    settings = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app):
        db = None
        if store is None:
            db = Database(settings)
            app.state.store = Store(db)
        else:
            app.state.store = store
        yield
        if db is not None:
            db.close()

    app = FastAPI(title="Shipping Scraper API", lifespan=lifespan)

    def get_store(request: Request) -> Store:
        return request.app.state.store

    def _key_matches(provided):
        return provided is not None and secrets.compare_digest(provided.encode(), settings.api_key.encode())

    def read_auth(key: str | None = Depends(_api_key_header)):
        if settings.api_key and not _key_matches(key):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or missing API key")

    def write_auth(key: str | None = Depends(_api_key_header)):
        if not settings.api_key:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "API_KEY is not configured on the server")
        if not _key_matches(key):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or missing API key")

    @app.exception_handler(psycopg2.OperationalError)
    async def db_unavailable(_request, exc):
        log.error("Database unavailable: %s", exc)
        return JSONResponse(status_code=503, content={"detail": "Database unavailable"})

    @app.exception_handler(Exception)
    async def unhandled(request, exc):
        log.exception("Unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(status_code=500, content={"detail": "Internal server error"})

    # ---- probes ------------------------------------------------------------

    @app.get("/health", include_in_schema=False)
    def health():
        """Liveness: the process is up."""
        return {"status": "ok"}

    @app.get("/ready", include_in_schema=False)
    def ready(store: Store = Depends(get_store)):
        """Readiness: the database is reachable."""
        try:
            store.db.ping()
        except psycopg2.Error as e:
            log.warning("Readiness check failed: %s", e)
            return JSONResponse(status_code=503, content={"status": "unavailable"})
        return {"status": "ok"}

    # ---- scrape triggers ---------------------------------------------------

    @app.post("/ships/{ship_id}/scrape", status_code=status.HTTP_202_ACCEPTED, dependencies=[Depends(write_auth)])
    def request_scrape(
        ship_id: int = Path(..., ge=1, le=MAX_SHIP_ID, description="MarineTraffic shipid"),
        store: Store = Depends(get_store),
    ):
        """Add the ship to the watch list and scrape it on the worker's next poll.

        Returns immediately; poll GET /ships/{ship_id}/status for the result.
        """
        store.request_scrape(ship_id)
        log.info("Scrape requested for %s", ship_id)
        return {"ship_id": ship_id, "status": "queued"}

    @app.get("/ships/{ship_id}/status", dependencies=[Depends(read_auth)])
    def scrape_status(
        ship_id: int = Path(..., ge=1, le=MAX_SHIP_ID),
        store: Store = Depends(get_store),
    ):
        """Watch-list entry and most recent scrape attempt for one ship."""
        result = store.scrape_status(ship_id)
        if result is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Ship is not on the watch list")
        return result

    # ---- read endpoints ----------------------------------------------------

    @app.get("/mainshipdata", dependencies=[Depends(read_auth)])
    def get_all_mainshipdata(
        limit: int = Query(MAX_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE, description="Rows per page (max 10)"),
        offset: int = Query(0, ge=0, description="Row offset"),
        store: Store = Depends(get_store),
    ):
        """Return all rows from mainshipdata with pagination (10 rows per page max)."""
        rows, total = store.get_all_mainshipdata(limit=limit, offset=offset)
        return {
            "total": total,
            "limit": limit,
            "offset": offset,
            "data": rows,
        }

    @app.get("/ship-inventory", dependencies=[Depends(read_auth)])
    def get_ship_inventory(
        limit: int = Query(MAX_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE, description="Rows per page (max 10)"),
        offset: int = Query(0, ge=0, description="Row offset"),
        store: Store = Depends(get_store),
    ):
        """Return the slim ship-inventory projection (vessel name, voyage ports/ETA,
        current area, type/subtype, last updated) with pagination (10 rows per page max)."""
        rows, total = store.get_ship_inventory(limit=limit, offset=offset)
        return {
            "total": total,
            "limit": limit,
            "offset": offset,
            "data": rows,
        }

    @app.get("/ship-inventory/search", dependencies=[Depends(read_auth)])
    def search_ship_inventory(
        q: str = Query(..., min_length=1, max_length=100, description="Vessel name or ship id fragment to search for"),
        limit: int = Query(MAX_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE, description="Rows per page (max 10)"),
        offset: int = Query(0, ge=0, description="Row offset"),
        store: Store = Depends(get_store),
    ):
        """Fuzzy-search the ship-inventory projection by vessel name or ship id.

        Typo-tolerant (pg_trgm similarity) and substring matching are combined,
        so "Evergren" or a partial id like "4521" both return hits. Results are
        ranked best match first.
        """
        rows, total = store.search_ship_inventory(q, limit=limit, offset=offset)
        return {
            "query": q,
            "total": total,
            "limit": limit,
            "offset": offset,
            "data": rows,
        }

    return app


def _build_default_app():
    settings = load_settings()
    setup_logging(settings.log_level, settings.log_format)
    if not settings.api_key:
        log.warning("API_KEY is not set: read endpoints are open and POST /ships/{id}/scrape is disabled")
    return create_app(settings)


app = _build_default_app()
