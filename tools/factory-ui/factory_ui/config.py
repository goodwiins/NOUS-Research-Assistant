"""Fixed settings. Nothing here is read from the environment or a request."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# The factory directory. Tests pass another root to make_server and Runner;
# no environment variable and no request parameter changes it.
FACTORY_ROOT = Path.home() / ".claude" / "factory"

HOST = "127.0.0.1"
DEFAULT_PORT = 9417

# Copied from the helper (VERIFY_DENY_PORTS): local services no test may reach.
VERIFY_DENY_PORTS = (
    *range(3000, 3011),
    *range(8000, 8011),
    5432,
    5433,
    6379,
    6333,
    6334,
    7474,
    7687,
    8080,
    8888,
    9000,
    11434,
    *range(54321, 54325),
)


def port_refusal(port: int) -> str | None:
    """Why `port` may not be used, or None when it may."""
    if port < 1024 or port > 49151:
        return "port must be between 1024 and 49151"
    if 3000 <= port <= 3010 or 8000 <= port <= 8010:
        return "port 3000-3010 and 8000-8010 belong to the app's dev servers"
    if 4100 <= port <= 4899:
        return "port 4100-4899 belongs to factory jobs and verify runs"
    if port in VERIFY_DENY_PORTS:
        return "port belongs to a local service the factory protects"
    return None


# The helper's default budget limits (LIMITS) and reading ages
# (USAGE_TRUST, USAGE_MAX_AGE). `F budget` does not print the limits.
LIMITS = {"five_hour": 75, "seven_day": 85}
USAGE_TRUST = 6 * 3600
USAGE_MAX_AGE = 1800

READ_CAP = 2 * 1024 * 1024
STDOUT_CAP = 1024 * 1024
STDERR_CAP = 2000
LOG_ROWS = 200
TITLE_CHARS = 100


@dataclass(frozen=True)
class Policy:
    """Cache TTL, minimum interval between runs and timeout, in seconds.
    `serve_stale`: answer with the old result while a refresh runs."""

    ttl: float
    min_interval: float
    timeout: float
    serve_stale: bool


COMMANDS = {
    "doctor": Policy(ttl=300, min_interval=60, timeout=120, serve_stale=True),
    "budget": Policy(ttl=15, min_interval=10, timeout=10, serve_stale=False),
}
