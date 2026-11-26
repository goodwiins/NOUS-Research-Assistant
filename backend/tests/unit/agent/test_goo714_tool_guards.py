"""GOO-714: load_project_skill requires verified current_user; do_kb_retrieve reports runtime_unavailable when db is None."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.services.agent.tools_impl import _dispatch_tool, _tool_do_kb_retrieve

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_load_project_skill_fails_closed_without_verified_current_user() -> None:
    db = MagicMock()
    with patch(
        "src.services.agent.runtime_snapshot.load_project_skill_from_snapshot",
        new_callable=AsyncMock,
    ) as mock_load:
        result = await _dispatch_tool(
            "load_project_skill",
            {"skill_name": "review"},
            user_id="raw-config-user-id",
            db=db,
            current_user=None,
            project_id="proj-1",
            runtime_snapshot_id="snap-1",
        )

    assert "authentication required" in result.get("error", "")
    mock_load.assert_not_awaited()


@pytest.mark.asyncio
async def test_load_project_skill_passes_verified_current_user_id() -> None:
    db = MagicMock()
    current_user = MagicMock()
    current_user.id = "verified-user-id"

    with patch(
        "src.services.agent.runtime_snapshot.load_project_skill_from_snapshot",
        new_callable=AsyncMock,
        return_value={"skill_name": "review", "instructions": "Check invariants."},
    ) as mock_load:
        result = await _dispatch_tool(
            "load_project_skill",
            {"skill_name": "review"},
            user_id="stale-raw-config-user-id",
            db=db,
            current_user=current_user,
            project_id="proj-1",
            runtime_snapshot_id="snap-1",
        )

    assert result["skill_name"] == "review"
    mock_load.assert_awaited_once_with(
        db,
        snapshot_id="snap-1",
        user_id="verified-user-id",
        project_id="proj-1",
        skill_name="review",
    )


@pytest.mark.asyncio
async def test_do_kb_retrieve_reports_runtime_unavailable_when_db_is_none() -> None:
    user = MagicMock()
    user.organization_id = "org-1"

    with patch(
        "src.core.config.settings",
        MagicMock(DO_KB_ENABLED=True, BEDROCK_KB_ID=""),
    ):
        result = await _tool_do_kb_retrieve({"query": "attention"}, None, user)

    assert result["chunks"] == []
    assert result["total"] == 0
    assert result["source"] == "do_kb"
    assert result["reason"] == "runtime_unavailable"
    assert result.get("error")


@pytest.mark.asyncio
async def test_do_kb_retrieve_still_reports_not_provisioned_when_db_present_without_kb() -> (
    None
):
    user = MagicMock()
    user.organization_id = "org-1"

    db = MagicMock()
    db.get = AsyncMock(return_value=MagicMock(do_kb_uuid=None))

    with patch(
        "src.core.config.settings",
        MagicMock(DO_KB_ENABLED=True, BEDROCK_KB_ID=""),
    ):
        result = await _tool_do_kb_retrieve({"query": "attention"}, db, user)

    assert result["chunks"] == []
    assert result["reason"] == "not_provisioned"
