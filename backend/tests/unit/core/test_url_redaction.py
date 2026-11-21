"""Connection URLs must never reach logs with credentials (GOO-634, CI-1).

``core/rate_limit.py`` logged the whole ``REDIS_URL`` at INFO on import and
``tasks/celery_app.py`` logged its first 20 characters; both can carry the
password. Only scheme, host and port may be logged.
"""

from __future__ import annotations

import logging

import pytest

from src.core.url_redaction import redact_url

pytestmark = pytest.mark.unit

SECRET = "s3cr3t-pa55"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (f"redis://:{SECRET}@cache.internal:6379/0", "redis://cache.internal:6379"),
        (
            f"rediss://default:{SECRET}@x.example.com:25061",
            "rediss://x.example.com:25061",
        ),
        (f"redis://user:{SECRET}@host/2?ssl_cert_reqs=none", "redis://host"),
        ("redis://localhost:6379/0", "redis://localhost:6379"),
        (f"postgresql://u:{SECRET}@[::1]:5432/db", "postgresql://[::1]:5432"),
    ],
)
def test_redact_url_keeps_only_scheme_host_port(url: str, expected: str) -> None:
    assert redact_url(url) == expected


@pytest.mark.parametrize(
    "url", ["", None, "not a url", f"{SECRET}", "redis://:bad@host:notaport"]
)
def test_redact_url_never_echoes_unparseable_input(url: object) -> None:
    out = redact_url(url)  # type: ignore[arg-type]
    assert SECRET not in out
    assert "bad" not in out


def test_rate_limiter_factory_does_not_log_password(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from src.core import rate_limit

    monkeypatch.setattr(
        rate_limit.settings, "REDIS_URL", f"redis://:{SECRET}@cache.internal:6379/0"
    )
    with caplog.at_level(logging.DEBUG, logger=rate_limit.logger.name):
        rate_limit.create_rate_limiter(5, 1)
    assert caplog.records, "expected the factory to log its backend"
    assert SECRET not in caplog.text
    assert "cache.internal:6379" in caplog.text


def test_celery_app_broker_log_line_is_redacted() -> None:
    """The module-level log line must go through the redaction helper."""
    import importlib
    import inspect

    # ``src.tasks.celery_app`` as an attribute is the Celery app object.
    source = inspect.getsource(importlib.import_module("src.tasks.celery_app"))
    assert "settings.REDIS_URL[:" not in source
    assert "redact_url(settings.REDIS_URL)" in source
