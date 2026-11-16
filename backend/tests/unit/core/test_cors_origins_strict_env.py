"""GOO-507: credentialed CORS must not trust loopback origins on a shared env.

The live AWS dev overlay listed ``http://localhost:3000`` /
``http://127.0.0.1:3000`` in ``CORS_ORIGINS``, and ``main.py`` registers
``CORSMiddleware(allow_credentials=True)``: any page served from a developer's
localhost could make credentialed requests to the live API. In a strict
(non-throwaway) environment ``cors_origins_list`` drops loopback origins; local
and CI environments keep them.
"""

import pytest

from src.core.config import settings

LIVE = "https://goodwiinz.tech"
LOOPBACK = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "https://localhost",
    "http://[::1]:3000",
    "http://LOCALHOST:3000",
]


@pytest.mark.unit
@pytest.mark.parametrize("env", ["dev", "production", "staging"])
def test_strict_env_drops_loopback_origins(
    monkeypatch: pytest.MonkeyPatch, env: str
) -> None:
    monkeypatch.setattr(settings, "ENVIRONMENT", env)
    monkeypatch.setattr(settings, "CORS_ORIGINS", ",".join([*LOOPBACK, LIVE]))

    assert settings.cors_origins_list == [LIVE]


@pytest.mark.unit
def test_strict_env_empty_origins_does_not_fall_back_to_localhost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "ENVIRONMENT", "dev")
    monkeypatch.setattr(settings, "CORS_ORIGINS", "")

    assert settings.cors_origins_list == []


@pytest.mark.unit
def test_strict_env_keeps_lookalike_public_hosts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lookalike = "https://localhost.example.com"
    monkeypatch.setattr(settings, "ENVIRONMENT", "dev")
    monkeypatch.setattr(settings, "CORS_ORIGINS", f"{lookalike},{LIVE}")

    assert settings.cors_origins_list == [lookalike, LIVE]


@pytest.mark.unit
@pytest.mark.parametrize("env", ["development", "testing", "local", "test", "ci"])
def test_throwaway_env_keeps_loopback_origins(
    monkeypatch: pytest.MonkeyPatch, env: str
) -> None:
    monkeypatch.setattr(settings, "ENVIRONMENT", env)
    monkeypatch.setattr(settings, "CORS_ORIGINS", ",".join([*LOOPBACK, LIVE]))

    assert settings.cors_origins_list == [*LOOPBACK, LIVE]
