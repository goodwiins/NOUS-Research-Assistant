"""A refused delivery must not leave the report marked "completed" (GOO-586).

_deliver_report swallowed every per-method error, so a webhook refused by the
SSRF guard still ended with last_run_status="completed" and no error, and the
user saw a completed report that was never delivered.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator
from unittest.mock import AsyncMock, patch

import pytest

from src.services.analytics.report_service import ReportGenerationService

_REFUSED = {"methods": ["webhook"], "webhook": {"url": "https://169.254.169.254/x"}}


class _FakeSession:
    def __init__(self, report: Any) -> None:
        self._report = report
        self.updates: list[dict[str, Any]] = []

    async def execute(self, stmt: Any) -> Any:
        if stmt.is_select:
            return SimpleNamespace(scalar_one_or_none=lambda: self._report)
        self.updates.append(
            {col.key: val.value for col, val in stmt._values.items()}  # noqa: SLF001
        )
        return None

    async def commit(self) -> None:
        pass


def _report(delivery_config: dict) -> Any:
    return SimpleNamespace(
        id=uuid.uuid4(),
        name="n",
        title="t",
        output_format="json",
        is_active=True,
        last_run_at=None,
        report_config={"sections": []},
        delivery_config=delivery_config,
    )


async def _run(
    service: ReportGenerationService, report: Any, tmp_path: Path
) -> tuple[bool, dict[str, Any]]:
    db = _FakeSession(report)

    @asynccontextmanager
    async def session() -> AsyncIterator[_FakeSession]:
        yield db

    out = tmp_path / "r.json"
    out.write_text("{}")
    with (
        patch("src.services.analytics.report_service.get_async_session", session),
        patch.object(service, "_generate_report_content", AsyncMock(return_value={})),
        patch.object(service, "_create_report_file", AsyncMock(return_value=out)),
    ):
        ok = await service.generate_report(report.id, force=True)
    return ok, db.updates[-1]


@pytest.mark.asyncio
async def test_refused_webhook_records_delivery_failure(tmp_path: Path) -> None:
    service = ReportGenerationService()
    ok, final = await _run(service, _report(_REFUSED), tmp_path)

    assert ok is False
    assert final["last_run_status"] == "delivery_failed"
    assert "webhook" in final["last_run_error"]
    assert "non-public" in final["last_run_error"]


@pytest.mark.asyncio
async def test_successful_delivery_still_completes(tmp_path: Path) -> None:
    service = ReportGenerationService()
    report = _report({"methods": ["s3"]})  # s3 is a logging no-op
    ok, final = await _run(service, report, tmp_path)

    assert ok is True
    assert final["last_run_status"] == "completed"
