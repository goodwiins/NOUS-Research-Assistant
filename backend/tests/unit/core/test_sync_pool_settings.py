"""GOO-693: the sync pool's sizing and wait time come from env per component."""

import pytest

from src.core import database

pytestmark = pytest.mark.unit


def test_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("DB_SYNC_POOL_SIZE", "DB_SYNC_MAX_OVERFLOW", "DB_SYNC_POOL_TIMEOUT"):
        monkeypatch.delenv(name, raising=False)
    assert database._sync_pool_settings() == {
        "pool_size": 3,
        "max_overflow": 2,
        "pool_timeout": 30,
    }


def test_timeout_is_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DB_SYNC_POOL_SIZE", "4")
    monkeypatch.setenv("DB_SYNC_MAX_OVERFLOW", "6")
    monkeypatch.setenv("DB_SYNC_POOL_TIMEOUT", "5")
    assert database._sync_pool_settings() == {
        "pool_size": 4,
        "max_overflow": 6,
        "pool_timeout": 5,
    }


def test_bad_timeout_keeps_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DB_SYNC_POOL_TIMEOUT", "soon")
    assert database._sync_pool_settings()["pool_timeout"] == 30
