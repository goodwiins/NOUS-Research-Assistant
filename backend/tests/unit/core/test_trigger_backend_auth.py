"""Trigger credentials stay scoped to the routes that consume them."""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.testclient import TestClient
from starlette.requests import Request

from src.core.config import settings
from src.core.dependencies import get_current_user
from src.core.security import TokenData, get_current_user_token
from src.middleware.multi_tenancy import _verified_token_data

_AGENT_KEY = "agent-task-signing-secret-at-least-32-chars"
_SERVICE_KEY = "trigger-service-signing-secret-32-chars"
_SERVICE_USER = "9f181a6c-dcc0-42d8-b810-b20ad1d42bee"


def _request(method: str, path: str) -> Request:
    return Request({"type": "http", "method": method, "path": path, "headers": []})


def _token(token_scope: str, key: str, sub: str, **claims: object) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "iss": "nous-trigger",
        "aud": "nous-backend",
        "sub": sub,
        "scope": token_scope,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=90)).timestamp()),
    }
    payload.update(claims)
    return jwt.encode(payload, key, algorithm="HS256")


def _authenticate(token: str, method: str, path: str) -> TokenData:
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
    return get_current_user_token(creds, request=_request(method, path))


@pytest.fixture(autouse=True)
def _trigger_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        settings, "TRIGGER_AGENT_SIGNING_KEY", _AGENT_KEY, raising=False
    )
    monkeypatch.setattr(
        settings, "TRIGGER_SERVICE_SIGNING_KEY", _SERVICE_KEY, raising=False
    )
    monkeypatch.setattr(
        settings, "TRIGGER_SERVICE_USER_ID", _SERVICE_USER, raising=False
    )


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/v1/agent/execute"),
        ("GET", "/api/v1/agent/jobs/aa768cc4-e32e-43c5-aeb6-7a4a3cee601e"),
        ("POST", "/api/v1/agent/confirm/aa768cc4-e32e-43c5-aeb6-7a4a3cee601e"),
    ],
)
def test_agent_ticket_authenticates_only_agent_job_routes(
    method: str, path: str
) -> None:
    token = _token("trigger:agent", _AGENT_KEY, "user-1")
    actor = _authenticate(token, method, path)
    assert actor.user_id == "user-1"
    assert actor.email is None
    assert actor.is_trigger is True


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/v1/threads"),
        ("POST", "/api/v1/agent/stream"),
        ("POST", "/api/v1/agent/execute/other"),
        ("DELETE", "/api/v1/agent/jobs/aa768cc4-e32e-43c5-aeb6-7a4a3cee601e"),
    ],
)
def test_agent_ticket_cannot_cross_its_boundary(method: str, path: str) -> None:
    token = _token("trigger:agent", _AGENT_KEY, "user-1")
    with pytest.raises(HTTPException) as exc:
        _authenticate(token, method, path)
    assert exc.value.status_code == 401


def test_service_ticket_authenticates_allowed_backend_call() -> None:
    token = _token(
        "trigger:service",
        _SERVICE_KEY,
        _SERVICE_USER,
        method="POST",
        path="/api/v1/arxiv/ingest",
        exp=int((datetime.now(timezone.utc) + timedelta(minutes=2)).timestamp()),
    )
    actor = _authenticate(token, "POST", "/api/v1/arxiv/ingest")
    assert actor.user_id == _SERVICE_USER
    assert actor.is_trigger is True


@pytest.mark.parametrize(
    ("claims", "method", "path"),
    [
        (
            {"method": "POST", "path": "/api/v1/arxiv/ingest"},
            "GET",
            "/api/v1/arxiv/ingest",
        ),
        ({"method": "POST", "path": "/api/v1/arxiv/ingest"}, "POST", "/api/v1/threads"),
        ({"method": "POST", "path": "/api/v1/threads"}, "POST", "/api/v1/threads"),
    ],
)
def test_service_ticket_cannot_change_method_or_path(
    claims: dict[str, object], method: str, path: str
) -> None:
    token = _token(
        "trigger:service",
        _SERVICE_KEY,
        _SERVICE_USER,
        exp=int((datetime.now(timezone.utc) + timedelta(minutes=2)).timestamp()),
        **claims,
    )
    with pytest.raises(HTTPException) as exc:
        _authenticate(token, method, path)
    assert exc.value.status_code == 401


@pytest.mark.parametrize(
    "claims",
    [
        {"exp": None},
        {"exp": 1},
        {"exp": int((datetime.now(timezone.utc) + timedelta(hours=3)).timestamp())},
        {"iat": 1},
        {"iat": int((datetime.now(timezone.utc) + timedelta(minutes=5)).timestamp())},
        {"scope": "cli"},
        {"aud": "authenticated"},
        {"iss": "attacker"},
    ],
)
def test_agent_ticket_rejects_invalid_claims(claims: dict[str, object]) -> None:
    token = _token("trigger:agent", _AGENT_KEY, "user-1", **claims)
    with pytest.raises(HTTPException) as exc:
        _authenticate(token, "POST", "/api/v1/agent/execute")
    assert exc.value.status_code == 401


def test_service_ticket_requires_configured_actor() -> None:
    token = _token(
        "trigger:service",
        _SERVICE_KEY,
        "other-user",
        method="POST",
        path="/api/v1/arxiv/ingest",
        exp=int((datetime.now(timezone.utc) + timedelta(minutes=2)).timestamp()),
    )
    with pytest.raises(HTTPException) as exc:
        _authenticate(token, "POST", "/api/v1/arxiv/ingest")
    assert exc.value.status_code == 401


def test_agent_ticket_rejects_wrong_signer() -> None:
    token = _token("trigger:agent", _SERVICE_KEY, "user-1")
    with pytest.raises(HTTPException) as exc:
        _authenticate(token, "POST", "/api/v1/agent/execute")
    assert exc.value.status_code == 401


def test_service_key_must_be_distinct_from_agent_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "TRIGGER_SERVICE_SIGNING_KEY", _AGENT_KEY)
    token = _token(
        "trigger:service",
        _AGENT_KEY,
        _SERVICE_USER,
        method="POST",
        path="/api/v1/arxiv/ingest",
        exp=int((datetime.now(timezone.utc) + timedelta(minutes=2)).timestamp()),
    )
    with pytest.raises(HTTPException) as exc:
        _authenticate(token, "POST", "/api/v1/arxiv/ingest")
    assert exc.value.status_code == 401


def test_agent_ticket_requires_configured_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "TRIGGER_AGENT_SIGNING_KEY", "")
    token = _token("trigger:agent", _AGENT_KEY, "user-1")
    with pytest.raises(HTTPException) as exc:
        _authenticate(token, "POST", "/api/v1/agent/execute")
    assert exc.value.status_code == 401


def test_tenant_middleware_accepts_only_scoped_trigger_ticket() -> None:
    token = _token("trigger:agent", _AGENT_KEY, "user-1")
    allowed = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/agent/execute",
            "headers": [(b"authorization", f"Bearer {token}".encode())],
        }
    )
    denied = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/agent/stream",
            "headers": [(b"authorization", f"Bearer {token}".encode())],
        }
    )
    allowed_data = _verified_token_data(allowed)
    assert allowed_data is not None
    assert allowed_data.user_id == "user-1"
    assert _verified_token_data(denied) is None


def test_fastapi_injects_request_into_auth_dependency() -> None:
    app = FastAPI()

    @app.get("/api/v1/agent/jobs/{job_id}")
    def job(
        actor: TokenData = Depends(get_current_user_token),
    ) -> dict[str, str | None]:
        return {"user_id": actor.user_id}

    token = _token("trigger:agent", _AGENT_KEY, "user-1")
    response = TestClient(app).get(
        "/api/v1/agent/jobs/aa768cc4-e32e-43c5-aeb6-7a4a3cee601e",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    assert response.json() == {"user_id": "user-1"}


def test_service_identity_must_already_exist() -> None:
    token = _token(
        "trigger:service",
        _SERVICE_KEY,
        _SERVICE_USER,
        method="POST",
        path="/api/v1/arxiv/ingest",
        exp=int((datetime.now(timezone.utc) + timedelta(minutes=2)).timestamp()),
    )
    actor = _authenticate(token, "POST", "/api/v1/arxiv/ingest")
    db = AsyncMock()
    db.execute.return_value = MagicMock()
    db.execute.return_value.scalars.return_value.first.return_value = None
    with patch("src.core.user_provisioning.ensure_user_and_org") as provision:
        with pytest.raises(HTTPException) as exc:
            asyncio.run(get_current_user(actor, db))
    assert exc.value.status_code == 401
    provision.assert_not_called()
