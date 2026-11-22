"""Network-boundary validation shared by Harbor eval tasks.

Consumed only by tasks added after 2026-08-07; the three original tasks are
digest-pinned and keep their inline copies. `validate_network_boundary` is the
parameterized port of the probe block in
`evals/agent-direct-project-action-v1/environment/run_agent.py`.
"""

from __future__ import annotations

import re
import socket
from typing import Any, Awaitable, Callable

import httpx

from .envelope import InfrastructureFailure


def _proxy_generated(response: "httpx.Response") -> bool:
    """True when the proxy itself produced the response (Squid error page)."""
    return "x-squid-error" in response.headers


def approved_probe_reachable(response: "httpx.Response") -> bool:
    """The approved model host answered through the proxy.

    Any 1xx-4xx status from the upstream counts (a model API root commonly
    answers 401/404). A 5xx, or any response the proxy generated itself
    (denied, upstream unreachable, gateway timeout), does not.
    """
    return 100 <= response.status_code <= 499 and not _proxy_generated(response)


def denied_probe_response_blocked(response: "httpx.Response") -> bool:
    """A response to the denied HTTPS probe means the CONNECT tunnel opened.

    The destination itself answering 403 is not a proxy block. Only a 403 the
    proxy generated counts.
    """
    return response.status_code == 403 and _proxy_generated(response)


def denied_probe_error_blocked(exc: Exception) -> bool:
    """httpx reports a refused CONNECT as ProxyError("<status> <reason>")."""
    return re.match(r"403\b", str(exc).strip()) is not None


async def validate_network_boundary(
    model_endpoint: str,
    extra_probes: dict[str, Callable[[], Awaitable[bool]]] | None = None,
) -> dict[str, Any]:
    """Prove main has no direct egress and proxy permits only the model host.

    Three fixed probes:
      * a raw socket to 1.1.1.1:443 must fail (no direct egress),
      * `model_endpoint` via the proxy must answer with an upstream 1xx-4xx
        (a 5xx or a proxy-generated error page is not reachability),
      * `https://example.com/` must be refused by the proxy: an
        `httpx.ProxyError` whose status is 403, or a proxy-generated 403.

    `extra_probes` maps an evidence key to an async callable returning a bool;
    each result is merged into the returned dict and must be truthy, otherwise
    the boundary check fails.

    Raises `InfrastructureFailure` on any boundary violation or ambiguous probe.
    """
    direct_blocked = False
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(2)
    try:
        sock.connect(("1.1.1.1", 443))
    except OSError:
        direct_blocked = True
    finally:
        sock.close()

    endpoint = (model_endpoint or "").strip()
    if not endpoint:
        raise InfrastructureFailure("model endpoint is missing")

    allowed_reachable = False
    blocked_status: int | None = None
    allowed_status: int | None = None
    try:
        with httpx.Client(timeout=10, follow_redirects=False) as client:
            allowed_response = client.get(endpoint.rstrip("/") + "/")
            allowed_status = allowed_response.status_code
            allowed_reachable = approved_probe_reachable(allowed_response)
    except httpx.HTTPError as exc:
        raise InfrastructureFailure(
            f"approved model-host proxy preflight failed: {type(exc).__name__}"
        ) from exc

    unrelated_blocked = False
    try:
        with httpx.Client(timeout=10, follow_redirects=False) as client:
            blocked_response = client.get("https://example.com/")
            blocked_status = blocked_response.status_code
            unrelated_blocked = denied_probe_response_blocked(blocked_response)
    except httpx.ProxyError as exc:
        # For a denied HTTPS CONNECT, httpx surfaces Squid's 403 as a
        # ProxyError instead of an HTTP response.  That is the expected proof
        # that the proxy rejected an unapproved destination.
        unrelated_blocked = denied_probe_error_blocked(exc)
        blocked_status = 403 if unrelated_blocked else None
    except httpx.HTTPError as exc:
        raise InfrastructureFailure(
            f"blocked-host proxy preflight failed ambiguously: {type(exc).__name__}"
        ) from exc

    result: dict[str, Any] = {
        "direct_public_socket_blocked": direct_blocked,
        "approved_model_host_reachable_via_proxy": allowed_reachable,
        "approved_model_probe_status": allowed_status,
        "unrelated_https_blocked_by_proxy": unrelated_blocked,
        "unrelated_probe_status": blocked_status,
    }

    extra_results: list[bool] = []
    for key, probe in (extra_probes or {}).items():
        try:
            outcome = bool(await probe())
        except InfrastructureFailure:
            raise
        except Exception as exc:  # noqa: BLE001 - a broken probe is infra, not reward 0
            raise InfrastructureFailure(
                f"extra network probe {key!r} failed: {type(exc).__name__}"
            ) from exc
        result[key] = outcome
        extra_results.append(outcome)

    if not all([direct_blocked, allowed_reachable, unrelated_blocked, *extra_results]):
        raise InfrastructureFailure(f"network boundary check failed: {result}")
    return result
