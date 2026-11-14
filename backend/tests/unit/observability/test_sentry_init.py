"""Sentry: probe traffic is never traced, and Celery reports its own errors.

On 2026-11-08 dev ran SENTRY_TRACES_SAMPLE_RATE=1.0, every kubelet probe became
a transaction, and the quota ran out. Celery never called ``init_sentry``, so
worker and beat failures never reached Sentry at all.

Mutation check: return ``rate`` for ``/health`` paths in ``_make_traces_sampler``,
or drop the ``celeryd_init`` receiver in ``celery_app.py``, and this file fails.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import MagicMock

import pytest

from src.observability import sentry

pytestmark = pytest.mark.unit


def _asgi(path: str) -> dict[str, Any]:
    return {"asgi_scope": {"type": "http", "path": path}}


@pytest.mark.parametrize(
    "path", ["/health", "/health/readiness", "/health/live", "/metrics"]
)
def test_probe_paths_are_never_traced(path: str) -> None:
    assert sentry._make_traces_sampler(1.0)(_asgi(path)) == 0.0


def test_real_requests_use_the_configured_rate() -> None:
    sampler = sentry._make_traces_sampler(0.1)
    assert sampler(_asgi("/api/v1/agent/execute")) == 0.1
    # Celery task transactions carry no ASGI scope.
    assert sampler({"celery_job": {"task": "x"}}) == 0.1


def test_continued_traces_keep_the_parent_decision() -> None:
    sampler = sentry._make_traces_sampler(0.1)
    assert sampler({**_asgi("/api/v1/x"), "parent_sampled": True}) == 1.0
    assert sampler({**_asgi("/api/v1/x"), "parent_sampled": False}) == 0.0


@pytest.fixture
def fresh_sentry(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    monkeypatch.setattr(sentry, "_initialized", False)
    init = MagicMock()
    monkeypatch.setattr(sentry.sentry_sdk, "init", init)
    monkeypatch.setattr(sentry.sentry_sdk, "set_tag", MagicMock())
    for name in ("SENTRY_DSN", "SENTRY_ENVIRONMENT", "ENVIRONMENT"):
        monkeypatch.delenv(name, raising=False)
    return init


def test_missing_dsn_warns_in_a_deployed_environment(
    fresh_sentry: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("SENTRY_ENVIRONMENT", "dev")

    with caplog.at_level(logging.INFO, logger=sentry.logger.name):
        assert sentry.init_sentry(service="nous-celery-worker") is False

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings and "nous-celery-worker" in warnings[0].getMessage()
    fresh_sentry.assert_not_called()


def test_missing_dsn_is_quiet_locally(
    fresh_sentry: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger=sentry.logger.name):
        assert sentry.init_sentry() is False

    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_init_uses_the_sampler_and_the_celery_integration(
    fresh_sentry: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SENTRY_DSN", "https://public@example.invalid/1")
    monkeypatch.setenv("SENTRY_ENVIRONMENT", "dev")
    monkeypatch.setenv("SENTRY_TRACES_SAMPLE_RATE", "0.1")

    assert sentry.init_sentry(service="nous-celery-beat") is True

    kwargs = fresh_sentry.call_args.kwargs
    assert "traces_sample_rate" not in kwargs
    assert kwargs["traces_sampler"](_asgi("/health")) == 0.0
    assert kwargs["traces_sampler"](_asgi("/api/v1/x")) == 0.1
    assert any(
        type(integration).__name__ == "CeleryIntegration"
        for integration in kwargs["integrations"]
    )
    sentry.sentry_sdk.set_tag.assert_called_with("service", "nous-celery-beat")


@pytest.mark.parametrize(
    ("signal_name", "service"),
    [("celeryd_init", "nous-celery-worker"), ("beat_init", "nous-celery-beat")],
)
def test_celery_processes_initialise_sentry(
    signal_name: str, service: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from celery import signals

    import src.tasks.celery_app  # noqa: F401 - registers the receivers

    calls: list[str] = []
    monkeypatch.setattr(
        sentry, "init_sentry", lambda service="nous-backend": calls.append(service)
    )

    getattr(signals, signal_name).send(sender=None)

    assert calls == [service]


def test_celery_init_failure_does_not_stop_the_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from celery import signals

    import src.tasks.celery_app  # noqa: F401 - registers the receivers

    def _boom(service: str = "nous-backend") -> bool:
        raise RuntimeError("bad dsn")

    monkeypatch.setattr(sentry, "init_sentry", _boom)

    signals.celeryd_init.send(sender=None)  # must not raise
