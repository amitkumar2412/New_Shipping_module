import pytest

from config import ConfigError, load_settings


def test_defaults(monkeypatch):
    s = load_settings()
    assert s.db_port == 5432
    assert s.sync_my_ships is True
    assert s.my_ships_table == "shipwithid"
    assert s.skip_recent_hours == 2
    assert s.max_run_minutes == 240


@pytest.mark.parametrize("name,value,message", [
    ("DB_HOST", "db.example.com/shipping", "bare hostname"),
    ("DB_PORT", "abc", "integer"),
    ("MAX_RUN_MINUTES", "5", ">= 10"),
    ("SKIP_RECENT_HOURS", "-1", ">= 0"),
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
