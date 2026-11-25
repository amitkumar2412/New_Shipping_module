import pytest

from config import ConfigError, load_settings


def test_defaults(monkeypatch):
    s = load_settings()
    assert s.db_port == 5432
    assert (s.my_ship_interval_minutes, s.other_ship_interval_minutes, s.stationary_interval_minutes) == (360, 1440, 600)
    assert s.max_ships_per_hour == 60
    assert s.sync_my_ships is True
    assert s.my_ships_table == "shipwithid"
    assert s.api_key is None


@pytest.mark.parametrize("name,value,message", [
    ("DB_HOST", "db.example.com/shipping", "bare hostname"),
    ("DB_PORT", "abc", "integer"),
    ("MY_SHIP_INTERVAL_MINUTES", "5", ">= 30"),
    ("MAX_SHIPS_PER_HOUR", "0", ">= 1"),
    ("SYNC_MY_SHIPS", "maybe", "true or false"),
    ("LOG_FORMAT", "xml", "text or json"),
    ("LOG_LEVEL", "LOUD", "LOG_LEVEL"),
    ("MY_SHIPS_TABLE", "ships; DROP TABLE x", "invalid characters"),
])
def test_invalid_values_fail_fast(monkeypatch, name, value, message):
    monkeypatch.setenv(name, value)
    with pytest.raises(ConfigError, match=message):
        load_settings()


def test_pool_bounds(monkeypatch):
    monkeypatch.setenv("DB_POOL_MIN", "5")
    monkeypatch.setenv("DB_POOL_MAX", "2")
    with pytest.raises(ConfigError, match="DB_POOL_MAX"):
        load_settings()
