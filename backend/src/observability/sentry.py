"""Sentry SDK initialization for the FastAPI backend.

Mirrors the frontend pattern: gated on `SENTRY_DSN`, no-op when unset, sample
rates and environment driven by env vars. The API calls it before the FastAPI
app is constructed; the Celery worker and beat call it from their startup
signals (``src/tasks/celery_app.py``).
"""

from __future__ import annotations

import logging
import os
from typing import Any

import sentry_sdk
from sentry_sdk.integrations.asyncio import AsyncioIntegration
from sentry_sdk.integrations.celery import CeleryIntegration
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.httpx import HttpxIntegration
from sentry_sdk.integrations.logging import LoggingIntegration
from sentry_sdk.integrations.redis import RedisIntegration
from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration
from sentry_sdk.integrations.starlette import StarletteIntegration

logger = logging.getLogger(__name__)

_initialized = False

# Environments where a missing DSN is expected; anywhere else it is a
# misconfiguration that leaves the service without error tracking.
_LOCAL_ENVIRONMENTS = frozenset({"development", "local", "test", "testing", "ci"})

# Kubelet probes and liveness checks hit these every few seconds. Tracing
# them at any rate spends the transaction quota on requests nobody debugs
# (dev ran at 1.0 and exhausted it on 2026-11-08).
_UNTRACED_PATH_PREFIXES = ("/health", "/metrics", "/favicon.ico")


def _make_traces_sampler(rate: float) -> Any:
    """Sample ``rate`` of transactions, never probe traffic.

    A continued trace keeps its parent's decision so distributed traces
    (API -> Celery) stay whole.
    """

    def _sampler(sampling_context: dict[str, Any]) -> float:
        parent_sampled = sampling_context.get("parent_sampled")
        if parent_sampled is not None:
            return 1.0 if parent_sampled else 0.0
        scope = sampling_context.get("asgi_scope") or {}
        path = str(scope.get("path") or "")
        if path.startswith(_UNTRACED_PATH_PREFIXES):
            return 0.0
        return rate

    return _sampler


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning(
            "Invalid float for %s=%r, falling back to %s", name, raw, default
        )
        return default


def _scrub_sensitive(event: dict[str, Any], _hint: dict[str, Any]) -> dict[str, Any]:
    """Strip auth headers/cookies before send. send_default_pii=False already
    drops most PII; this guarantees we never leak bearer tokens.
    """
    request = event.get("request") or {}
    headers = request.get("headers") or {}
    for key in list(headers.keys()):
        if key.lower() in {
            "authorization",
            "cookie",
            "x-api-key",
            "proxy-authorization",
        }:
            headers[key] = "[Filtered]"
    if "cookies" in request:
        request["cookies"] = "[Filtered]"
    return event


def init_sentry(service: str = "nous-backend") -> bool:
    """Initialize the Sentry SDK. Returns True if active, False if disabled.

    ``service`` tags every event (``nous-backend``, ``nous-celery-worker``,
    ``nous-celery-beat``) so the three processes can be told apart.

    Reads:
      - SENTRY_DSN              (required; absent => no-op)
      - SENTRY_ENVIRONMENT      (defaults to ENVIRONMENT, then "development")
      - SENTRY_TRACES_SAMPLE_RATE  (default 0.1 in prod, 1.0 elsewhere)
      - SENTRY_PROFILES_SAMPLE_RATE (default 0.0)
      - APP_VERSION             (release tag; defaults to "unknown")
    """
    global _initialized
    if _initialized:
        return True

    environment = (
        os.getenv("SENTRY_ENVIRONMENT") or os.getenv("ENVIRONMENT") or "development"
    )
    dsn = os.getenv("SENTRY_DSN")
    if not dsn:
        if environment.lower() in _LOCAL_ENVIRONMENTS:
            logger.info("Sentry DSN not set — error tracking disabled")
        else:
            logger.warning(
                "SENTRY_DSN is not set in environment %r: %s errors are not "
                "being reported",
                environment,
                service,
            )
        return False

    is_prod = environment.lower() in {"prod", "production"}
    traces_sample_rate = _float_env(
        "SENTRY_TRACES_SAMPLE_RATE", 0.1 if is_prod else 1.0
    )
    profiles_sample_rate = _float_env("SENTRY_PROFILES_SAMPLE_RATE", 0.0)
    release = os.getenv("APP_VERSION") or os.getenv("GIT_SHA") or "unknown"

    sentry_sdk.init(
        dsn=dsn,
        environment=environment,
        release=release,
        traces_sampler=_make_traces_sampler(traces_sample_rate),
        profiles_sample_rate=profiles_sample_rate,
        send_default_pii=False,
        attach_stacktrace=True,
        before_send=_scrub_sensitive,
        integrations=[
            StarletteIntegration(transaction_style="endpoint"),
            FastApiIntegration(transaction_style="endpoint"),
            SqlalchemyIntegration(),
            RedisIntegration(),
            AsyncioIntegration(),
            HttpxIntegration(),
            CeleryIntegration(),
            LoggingIntegration(level=logging.INFO, event_level=logging.ERROR),
        ],
    )
    sentry_sdk.set_tag("service", service)

    _initialized = True
    logger.info(
        "Sentry initialized (service=%s, env=%s, traces=%s, profiles=%s, release=%s)",
        service,
        environment,
        traces_sample_rate,
        profiles_sample_rate,
        release,
    )
    return True
