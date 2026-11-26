"""Settings read from environment variables (or a .env file).

Everything is read once into a frozen Settings object. Invalid values fail fast at
startup with a clear message instead of surfacing later as a confusing runtime error.
"""
import os
from dataclasses import dataclass


class ConfigError(Exception):
    pass


def _str(name, default=None, required=False):
    value = os.environ.get(name, "").strip()
    if not value:
        if required:
            raise ConfigError(f"{name} must be set")
        return default
    return value


def _int(name, default, minimum=None):
    raw = _str(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be an integer, got {raw!r}")
    if minimum is not None and value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {value}")
    return value


def _bool(name, default):
    raw = _str(name)
    if raw is None:
        return default
    if raw.lower() in ("1", "true", "yes", "on"):
        return True
    if raw.lower() in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{name} must be true or false, got {raw!r}")


@dataclass(frozen=True)
class Settings:
    # database
    db_host: str
    db_port: int
    db_name: str
    db_user: str
    db_password: str
    db_sslmode: str
    db_pool_min: int
    db_pool_max: int
    db_connect_timeout: int

    # scheduled run
    sync_my_ships: bool
    my_ships_table: str
    skip_recent_hours: int
    max_run_minutes: int
    max_consecutive_blocks: int

    # scraper
    scraper_proxy: str | None
    scraper_timeout: int

    # logging
    log_level: str
    log_format: str


def load_settings() -> Settings:
    db_host = _str("DB_HOST", "localhost")
    if "/" in db_host:
        raise ConfigError(f"DB_HOST must be a bare hostname, got {db_host!r} (put the database name in DB_NAME)")

    log_level = _str("LOG_LEVEL", "INFO").upper()
    if log_level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        raise ConfigError(f"LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR or CRITICAL, got {log_level!r}")

    log_format = _str("LOG_FORMAT", "text").lower()
    if log_format not in ("text", "json"):
        raise ConfigError(f"LOG_FORMAT must be text or json, got {log_format!r}")

    my_ships_table = _str("MY_SHIPS_TABLE", "shipwithid")
    if not my_ships_table.replace("_", "").replace(".", "").isalnum():
        raise ConfigError(f"MY_SHIPS_TABLE has invalid characters: {my_ships_table!r}")

    pool_min = _int("DB_POOL_MIN", 1, minimum=1)
    pool_max = _int("DB_POOL_MAX", 5, minimum=1)
    if pool_max < pool_min:
        raise ConfigError("DB_POOL_MAX must be >= DB_POOL_MIN")

    return Settings(
        db_host=db_host,
        db_port=_int("DB_PORT", 5432, minimum=1),
        db_name=_str("DB_NAME", "marinetraffic"),
        db_user=_str("DB_USER", "postgres"),
        db_password=_str("DB_PASSWORD", ""),
        db_sslmode=_str("DB_SSLMODE", "require"),
        db_pool_min=pool_min,
        db_pool_max=pool_max,
        db_connect_timeout=_int("DB_CONNECT_TIMEOUT", 10, minimum=1),
        sync_my_ships=_bool("SYNC_MY_SHIPS", True),
        my_ships_table=my_ships_table,
        skip_recent_hours=_int("SKIP_RECENT_HOURS", 2, minimum=0),
        max_run_minutes=_int("MAX_RUN_MINUTES", 240, minimum=10),
        max_consecutive_blocks=_int("MAX_CONSECUTIVE_BLOCKS", 3, minimum=1),
        scraper_proxy=_str("SCRAPER_PROXY"),
        scraper_timeout=_int("SCRAPER_TIMEOUT", 30, minimum=5),
        log_level=log_level,
        log_format=log_format,
    )
