"""GOO-434: the HITL lifecycle eval must send the approval receipt on /confirm.

#1842 made ``approval_id`` (64 hex) a required field of ``ConfirmationRequest``;
the adapter still posted ``{"confirmed": ...}`` and every phase got a 422.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "evals/agent-hitl-lifecycle-v1/environment/run_agent.py"
APPROVAL_ID = "a" * 64


@pytest.fixture(scope="module")
def run_agent():
    sys.path.insert(0, str(ROOT))
    try:
        spec = importlib.util.spec_from_file_location("hitl_run_agent", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.path.remove(str(ROOT))


@pytest.mark.unit
@pytest.mark.parametrize("confirmed", [True, False])
def test_confirm_job_sends_approval_id(run_agent, confirmed):
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append({"path": request.url.path, "body": json.loads(request.content)})
        return httpx.Response(200, json={"status": "ok"})

    async def go() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            await run_agent.confirm_job(
                c, {}, "job-1", confirmed=confirmed, approval_id=APPROVAL_ID
            )

    asyncio.run(go())

    assert seen == [
        {
            "path": "/api/v1/agent/confirm/job-1",
            "body": {"confirmed": confirmed, "approval_id": APPROVAL_ID},
        }
    ]


@pytest.mark.unit
def test_approval_id_read_from_parked_confirmation(run_agent):
    parked = {
        "status": "awaiting_confirmation",
        "confirmation": {"approval_id": APPROVAL_ID},
    }

    assert run_agent.approval_id_of(parked) == APPROVAL_ID


@pytest.mark.unit
@pytest.mark.parametrize(
    "parked",
    [{}, {"confirmation": None}, {"confirmation": {"approval_id": "not-hex"}}],
)
def test_missing_approval_id_is_an_infrastructure_failure(run_agent, parked):
    with pytest.raises(run_agent.InfrastructureFailure):
        run_agent.approval_id_of(parked)
