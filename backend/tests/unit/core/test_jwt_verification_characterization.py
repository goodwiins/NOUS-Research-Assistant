"""Characterization tests for every JWT decode path (GOO-474).

GOO-474 swapped ``python-jose`` for ``PyJWT``. These tests pin the decode
behaviour the swap had to keep: which algorithms each path accepts, the
audience and issuer checks, expiry with no leeway, JWKS key handling, and
how each failure maps to ``None`` / 401 / a WebSocket close code / an
``AuthenticationError``. Tokens are minted with PyJWT or built by hand, so
the tests do not depend on the library under test.

The places where PyJWT is stricter than jose by design are pinned at the end
of the file (``test_tightened_*``).
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict
from unittest.mock import MagicMock

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

import src.core.security as security
from src.core.config import settings
from src.core.security import (
    _CLI_TOKEN_ISSUER,
    _CLI_TOKEN_SCOPE,
    get_current_user_token,
    verify_token,
)
from src.core.websocket_auth import WebSocketAuthenticator, WebSocketAuthError

pytestmark = pytest.mark.unit

_SUPABASE_SECRET = "characterization-supabase-shared-secret-48-bytes-long!!"
_SUPABASE_ISSUER = "https://characterization.supabase.co/auth/v1"
_KID = "char-kid"


def _now() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _supabase_claims(**overrides: Any) -> Dict[str, Any]:
    claims: Dict[str, Any] = {
        "sub": "sb-user",
        "email": "sb@example.com",
        "app_metadata": {"role": "USER"},
        "aud": "authenticated",
        "exp": _now() + 600,
    }
    claims.update(overrides)
    return {k: v for k, v in claims.items() if v is not None}


def _hs(
    claims: Dict[str, Any], secret: str = _SUPABASE_SECRET, alg: str = "HS256"
) -> str:
    return str(jwt.encode(claims, secret, algorithm=alg))


def _cli_claims(**overrides: Any) -> Dict[str, Any]:
    claims: Dict[str, Any] = {
        "sub": "cli-user",
        "email": "cli@example.com",
        "app_metadata": {"organization_id": "org-1", "role": "USER"},
        "scope": _CLI_TOKEN_SCOPE,
        "iss": _CLI_TOKEN_ISSUER,
        "iat": _now(),
        "exp": _now() + 600,
    }
    claims.update(overrides)
    return {k: v for k, v in claims.items() if v is not None}


def _unsigned(claims: Dict[str, Any], alg: str = "none") -> str:
    header = _b64(json.dumps({"alg": alg, "typ": "JWT"}).encode())
    payload = _b64(json.dumps(claims).encode())
    return f"{header}.{payload}."


@pytest.fixture(autouse=True)
def _settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "SUPABASE_JWT_SECRET", _SUPABASE_SECRET)
    monkeypatch.setattr(settings, "SUPABASE_JWT_ISSUER", "")
    monkeypatch.setattr(settings, "JWT_ALGORITHM", "HS256")
    # No network: the JWKS path sees no keys unless a test installs some.
    monkeypatch.setattr(security, "_get_supabase_jwks", lambda: None)


@pytest.fixture
def es256(monkeypatch: pytest.MonkeyPatch) -> ec.EllipticCurvePrivateKey:
    """Install a one-key JWKS and return its private key."""
    priv = ec.generate_private_key(ec.SECP256R1())
    pub_jwk = jwt.algorithms.ECAlgorithm.to_jwk(priv.public_key(), as_dict=True)
    pub_jwk.update({"kid": _KID, "alg": "ES256", "use": "sig"})
    monkeypatch.setattr(security, "_get_supabase_jwks", lambda: {"keys": [pub_jwk]})
    return priv


def _es(priv: Any, claims: Dict[str, Any], kid: str | None = _KID) -> str:
    headers = {"kid": kid} if kid is not None else None
    return str(jwt.encode(claims, priv, algorithm="ES256", headers=headers))


# ---------------------------------------------------------------------------
# verify_token: Supabase HS256 (shared secret)
# ---------------------------------------------------------------------------


def test_hs256_valid_token_accepted() -> None:
    data = verify_token(_hs(_supabase_claims()))
    assert data is not None and data.user_id == "sb-user" and not data.is_cli


def test_hs256_audience_list_containing_authenticated_accepted() -> None:
    token = _hs(_supabase_claims(aud=["other", "authenticated"]))
    assert verify_token(token) is not None


@pytest.mark.parametrize("aud", ["anon", "service_role", ["other"]])
def test_hs256_wrong_audience_rejected(aud: Any) -> None:
    assert verify_token(_hs(_supabase_claims(aud=aud))) is None


def test_hs256_wrong_signature_rejected() -> None:
    token = _hs(_supabase_claims(), secret="another-secret-of-sufficient-length-1234")
    assert verify_token(token) is None


def test_hs256_tampered_payload_rejected() -> None:
    header, _payload, sig = _hs(_supabase_claims()).split(".")
    forged = _b64(json.dumps(_supabase_claims(sub="someone-else")).encode())
    assert verify_token(f"{header}.{forged}.{sig}") is None


@pytest.mark.parametrize("alg", ["HS384", "HS512"])
def test_hs256_path_rejects_other_hmac_algorithms(alg: str) -> None:
    """Same secret, different HMAC alg: the allow-list is HS256 only."""
    assert verify_token(_hs(_supabase_claims(), alg=alg)) is None


@pytest.mark.parametrize("alg", ["none", "None", "NONE"])
def test_unsigned_alg_none_token_rejected(alg: str) -> None:
    assert verify_token(_unsigned(_supabase_claims(), alg=alg)) is None
    assert verify_token(_unsigned(_cli_claims(), alg=alg)) is None


def test_hs256_expired_rejected_without_leeway() -> None:
    assert verify_token(_hs(_supabase_claims(exp=_now() - 2))) is None


def test_hs256_not_yet_valid_nbf_rejected() -> None:
    assert verify_token(_hs(_supabase_claims(nbf=_now() + 600))) is None


def test_hs256_future_iat_still_accepted() -> None:
    """jose never rejected a future ``iat``; a Supabase clock a little ahead
    of ours must not turn a fresh login into a 401."""
    assert verify_token(_hs(_supabase_claims(iat=_now() + 120))) is not None


def test_hs256_non_numeric_iat_rejected() -> None:
    assert verify_token(_hs(_supabase_claims(iat="yesterday"))) is None


def test_hs256_non_string_sub_rejected() -> None:
    assert verify_token(_hs(_supabase_claims(sub=12345))) is None


def test_hs256_issuer_pinned_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "SUPABASE_JWT_ISSUER", _SUPABASE_ISSUER)
    assert verify_token(_hs(_supabase_claims(iss=_SUPABASE_ISSUER))) is not None
    assert verify_token(_hs(_supabase_claims(iss="https://evil/auth/v1"))) is None
    assert verify_token(_hs(_supabase_claims())) is None  # iss missing


# ---------------------------------------------------------------------------
# verify_token: Supabase ES256 (JWKS)
# ---------------------------------------------------------------------------


def test_es256_valid_token_accepted(es256: Any) -> None:
    data = verify_token(_es(es256, _supabase_claims(sub="es-user")))
    assert data is not None and data.user_id == "es-user"


def test_es256_token_without_kid_tries_keys(es256: Any) -> None:
    assert verify_token(_es(es256, _supabase_claims(), kid=None)) is not None


def test_es256_unknown_kid_rejected(es256: Any) -> None:
    assert verify_token(_es(es256, _supabase_claims(), kid="other-kid")) is None


def test_es256_wrong_signing_key_rejected(es256: Any) -> None:
    attacker = ec.generate_private_key(ec.SECP256R1())
    assert verify_token(_es(attacker, _supabase_claims())) is None


def test_es256_wrong_audience_rejected(es256: Any) -> None:
    assert verify_token(_es(es256, _supabase_claims(aud="anon"))) is None


def test_es256_expired_rejected(es256: Any) -> None:
    assert verify_token(_es(es256, _supabase_claims(exp=_now() - 2))) is None


def test_es256_future_iat_still_accepted(es256: Any) -> None:
    assert verify_token(_es(es256, _supabase_claims(iat=_now() + 120))) is not None


def test_es256_issuer_pinned_when_configured(
    es256: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "SUPABASE_JWT_ISSUER", _SUPABASE_ISSUER)
    assert verify_token(_es(es256, _supabase_claims(iss=_SUPABASE_ISSUER))) is not None
    assert verify_token(_es(es256, _supabase_claims(iss="https://evil"))) is None


def test_es384_token_rejected_even_with_matching_kid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The JWKS path only runs for header alg ES256 and decodes with
    ``algorithms=["ES256"]``; the token header never picks the algorithm."""
    priv = ec.generate_private_key(ec.SECP384R1())
    pub_jwk = jwt.algorithms.ECAlgorithm.to_jwk(priv.public_key(), as_dict=True)
    pub_jwk.update({"kid": _KID, "alg": "ES384"})
    monkeypatch.setattr(security, "_get_supabase_jwks", lambda: {"keys": [pub_jwk]})
    token = jwt.encode(
        _supabase_claims(), priv, algorithm="ES384", headers={"kid": _KID}
    )
    assert verify_token(token) is None


def test_es256_header_with_non_ec_jwk_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """A JWKS entry that is not an EC P-256 key can never verify an ES256
    token, whatever its ``alg`` field says."""
    oct_jwk = {"kty": "oct", "k": _b64(b"x" * 32), "kid": _KID, "alg": "HS256"}
    monkeypatch.setattr(security, "_get_supabase_jwks", lambda: {"keys": [oct_jwk]})
    priv = ec.generate_private_key(ec.SECP256R1())
    assert verify_token(_es(priv, _supabase_claims())) is None


# ---------------------------------------------------------------------------
# verify_token: CLI tokens (HS256 with JWT_SECRET_KEY)
# ---------------------------------------------------------------------------


def _cli(claims: Dict[str, Any], alg: str = "HS256") -> str:
    return str(jwt.encode(claims, settings.JWT_SECRET_KEY, algorithm=alg))


def test_cli_valid_token_accepted() -> None:
    data = verify_token(_cli(_cli_claims()))
    assert data is not None and data.is_cli and data.issued_at is not None


def test_cli_token_audience_not_checked() -> None:
    """The CLI path decodes with ``verify_aud=False``."""
    data = verify_token(_cli(_cli_claims(aud="anything")))
    assert data is not None and data.is_cli


def test_cli_token_wrong_issuer_rejected() -> None:
    assert verify_token(_cli(_cli_claims(iss="someone-else"))) is None


def test_cli_token_missing_issuer_rejected() -> None:
    assert verify_token(_cli(_cli_claims(iss=None))) is None


@pytest.mark.parametrize("alg", ["HS384", "HS512"])
def test_cli_token_other_hmac_algorithm_rejected(alg: str) -> None:
    assert verify_token(_cli(_cli_claims(), alg=alg)) is None


def test_cli_token_expired_rejected() -> None:
    assert verify_token(_cli(_cli_claims(exp=_now() - 2))) is None


def test_cli_token_non_numeric_iat_rejected() -> None:
    assert verify_token(_cli(_cli_claims(iat="yesterday"))) is None


# ---------------------------------------------------------------------------
# Malformed input and the HTTP chokepoint
# ---------------------------------------------------------------------------

_MALFORMED = [
    "",
    "not-a-jwt",
    "a.b",
    "a.b.c",
    "a.b.c.d",
    _b64(b'{"alg":"HS256"}') + ".",
    _b64(b'{"alg":"HS256"}') + "." + _b64(b"not json") + ".sig",
    _b64(b'{"alg":"HS256"}') + "." + _b64(b"[1,2]") + ".sig",
    _b64(b"[]") + "." + _b64(b"{}") + ".sig",
    "Bearer " + "x" * 20,
]


@pytest.mark.parametrize("token", _MALFORMED)
def test_malformed_token_returns_none(token: str) -> None:
    assert verify_token(token) is None


@pytest.mark.parametrize(
    "token",
    _MALFORMED
    + [
        _hs(_supabase_claims(exp=_now() - 2)),
        _hs(_supabase_claims(aud="anon")),
        _hs(_supabase_claims(), alg="HS512"),
        _unsigned(_supabase_claims()),
    ],
)
def test_every_rejection_maps_to_401(token: str) -> None:
    with pytest.raises(HTTPException) as exc:
        get_current_user_token(
            HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
        )
    assert exc.value.status_code == 401
    assert exc.value.headers == {"WWW-Authenticate": "Bearer"}


# ---------------------------------------------------------------------------
# src/core/websocket_auth.py: close codes
# ---------------------------------------------------------------------------


class _WS:
    def __init__(self, token: str) -> None:
        self.headers = {"authorization": f"Bearer {token}"}
        self.cookies: Dict[str, str] = {}


async def _ws_code(token: str) -> int:
    with pytest.raises(WebSocketAuthError) as exc:
        await WebSocketAuthenticator.authenticate(_WS(token))  # type: ignore[arg-type]
    return int(exc.value.code)


async def test_ws_valid_token_authenticates() -> None:
    payload = await WebSocketAuthenticator.authenticate(
        _WS(_hs(_supabase_claims()))  # type: ignore[arg-type]
    )
    assert payload["sub"] == "sb-user"


async def test_ws_expired_token_closes_4002() -> None:
    assert await _ws_code(_hs(_supabase_claims(exp=_now() - 60))) == 4002


async def test_ws_expired_token_with_bad_signature_still_closes_4002() -> None:
    """The 4002/4003 split reads the unverified ``exp`` only to pick the close
    code; the connection is refused either way."""
    token = _hs(_supabase_claims(exp=_now() - 60), secret="x" * 40)
    assert await _ws_code(token) == 4002


@pytest.mark.parametrize(
    "token",
    ["garbage", "a.b.c", _hs(_supabase_claims(aud="anon"))],
)
async def test_ws_invalid_token_closes_4003(token: str) -> None:
    assert await _ws_code(token) == 4003


# ---------------------------------------------------------------------------
# src/websocket/auth.py: legacy authenticator's own Supabase decode
# ---------------------------------------------------------------------------


@pytest.fixture
def legacy_ws() -> Any:
    from src.websocket.auth import WebSocketAuthenticator as LegacyAuthenticator

    return LegacyAuthenticator()


async def test_legacy_ws_valid_token_maps_role(legacy_ws: Any) -> None:
    payload = await legacy_ws._validate_jwt_token(
        _hs(_supabase_claims(app_metadata={"role": "ADMIN"}))
    )
    assert payload is not None
    assert payload["sub"] == "sb-user" and payload["role"] == "ADMIN"


@pytest.mark.parametrize(
    "token",
    [
        _hs(_supabase_claims(exp=_now() - 2)),
        _hs(_supabase_claims(aud="anon")),
        _hs(_supabase_claims(), secret="y" * 40),
        _hs(_supabase_claims(), alg="HS512"),
        _unsigned(_supabase_claims()),
        _hs(_supabase_claims(sub=None)),
        "garbage",
    ],
)
async def test_legacy_ws_rejections_return_none(legacy_ws: Any, token: str) -> None:
    assert await legacy_ws._validate_jwt_token(token) is None


async def test_legacy_ws_future_iat_still_accepted(legacy_ws: Any) -> None:
    token = _hs(_supabase_claims(iat=_now() + 120))
    assert await legacy_ws._validate_jwt_token(token) is not None


async def test_legacy_ws_refresh_token_round_trips(legacy_ws: Any) -> None:
    token = await legacy_ws._generate_refresh_token("user-1", "org-1")
    claims = jwt.decode(
        token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM]
    )
    assert claims["sub"] == "user-1" and claims["type"] == "websocket_refresh"
    assert isinstance(claims["exp"], int) and isinstance(claims["iat"], int)


# ---------------------------------------------------------------------------
# src/services/security/user_management.py: AuthService
# ---------------------------------------------------------------------------


@pytest.fixture
def auth_service() -> Any:
    from src.services.security.user_management import AuthService

    return AuthService()


async def test_auth_service_access_token_round_trips(auth_service: Any) -> None:
    token = await auth_service.create_access_token({"sub": "u-1"})
    payload = await auth_service.verify_token(token, "access")
    assert payload["sub"] == "u-1" and payload["type"] == "access"
    assert isinstance(payload["exp"], int) and isinstance(payload["iat"], int)


async def test_auth_service_expired_token(auth_service: Any) -> None:
    from src.shared.exceptions import AuthenticationError

    token = await auth_service.create_access_token(
        {"sub": "u-1"}, expires_delta=timedelta(seconds=-5)
    )
    with pytest.raises(AuthenticationError, match="Token has expired"):
        await auth_service.verify_token(token, "access")


@pytest.mark.parametrize(
    "token",
    [
        "garbage",
        _unsigned({"sub": "u", "type": "access", "exp": _now() + 60}),
    ],
)
async def test_auth_service_invalid_token(auth_service: Any, token: str) -> None:
    from src.shared.exceptions import AuthenticationError

    with pytest.raises(AuthenticationError, match="^Invalid token"):
        await auth_service.verify_token(token, "access")


async def test_auth_service_wrong_algorithm(auth_service: Any) -> None:
    from src.shared.exceptions import AuthenticationError

    token = jwt.encode(
        {"sub": "u", "type": "access", "exp": _now() + 60},
        settings.JWT_SECRET_KEY,
        algorithm="HS512",
    )
    with pytest.raises(AuthenticationError, match="^Invalid token"):
        await auth_service.verify_token(token, "access")


# ---------------------------------------------------------------------------
# src/security/enhanced_auth.py: TokenManager (already PyJWT; its except
# clauses named jose's ``JWTError``, which PyJWT does not have)
# ---------------------------------------------------------------------------


def test_enhanced_token_manager_rejects_garbage() -> None:
    from src.security.enhanced_auth import TokenManager

    manager = TokenManager(MagicMock())
    assert manager.validate_token("garbage") is None
    assert manager.revoke_token("garbage") is False


# ---------------------------------------------------------------------------
# Deliberate tightenings (PyJWT is stricter than jose here)
# ---------------------------------------------------------------------------


def test_tightened_supabase_token_without_aud_rejected(es256: Any) -> None:
    """jose skipped the audience check when the token had no ``aud`` claim;
    PyJWT requires the claim once an audience is expected. Real Supabase
    access tokens always carry ``aud="authenticated"``."""
    assert verify_token(_hs(_supabase_claims(aud=None))) is None
    assert verify_token(_es(es256, _supabase_claims(aud=None))) is None


async def test_tightened_legacy_ws_token_without_aud_rejected(legacy_ws: Any) -> None:
    token = _hs(_supabase_claims(aud=None))
    assert await legacy_ws._validate_jwt_token(token) is None


def test_tightened_token_expiring_this_second_rejected() -> None:
    """jose accepted ``exp == now``; PyJWT treats it as expired."""
    assert verify_token(_hs(_supabase_claims(exp=_now()))) is None
