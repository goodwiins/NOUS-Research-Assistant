"""GOO-777: ``PATCH /matrices/{id}`` reads the release graph only for a column
change (GOO-721).

A rename alone (``columns is None``) appends no form version, so it cannot
stale a release: it resolves with plain ``resolve_project`` and never takes
the pre-lock graph snapshot (``draft_release_service.resolve_for_staling``).
A column change does, because ``create_version`` stales releases built on
the old fields.

Mutation check: resolving with ``resolve_for_staling`` unconditionally (or
on ``columns is None``) in ``update_matrix`` makes
``test_a_rename_alone_skips_the_release_snapshot`` fail; resolving with
``resolve_project`` unconditionally makes
``test_a_column_change_takes_the_release_snapshot`` fail.
"""

from __future__ import annotations

from importlib import import_module
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from src.services.research import draft_release_service
from src.services.research import extraction_forms_service as forms
from src.services.research_engine.project_access import ResearchAction
from src.shared.scispace_schemas import UpdateMatrixRequest

pytestmark = pytest.mark.unit


def _routes() -> Any:
    return import_module("src.api.research.extraction_matrix")


class _World(SimpleNamespace):
    db: AsyncMock
    resolved: list[str]
    versions: list[Any]
    matrix: SimpleNamespace


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> _World:
    project_id = uuid4()
    matrix = SimpleNamespace(
        id=uuid4(),
        project_id=project_id,
        name="Old",
        columns=[{"name": "design"}],
        updated_at=None,
    )
    found = MagicMock()
    found.scalar_one_or_none.return_value = matrix
    db = AsyncMock()
    db.execute.return_value = found
    w = _World(db=db, resolved=[], versions=[], matrix=matrix)

    def resolver(label: str) -> Any:
        async def resolve(
            _db: Any, pid: UUID, _user_id: UUID, action: ResearchAction
        ) -> Any:
            assert pid == project_id and action is ResearchAction.EDIT
            w.resolved.append(label)
            return SimpleNamespace(collection=SimpleNamespace(id=pid))

        return resolve

    async def create_version(
        _db: Any, _context: Any, _matrix: Any, columns: Any, _u: Any
    ) -> None:
        w.versions.append(columns)

    monkeypatch.setattr(_routes(), "resolve_project", resolver("resolve_project"))
    monkeypatch.setattr(
        draft_release_service, "resolve_for_staling", resolver("resolve_for_staling")
    )
    monkeypatch.setattr(forms, "create_version", create_version)
    return w


async def _patch(w: _World, request: UpdateMatrixRequest) -> dict[str, Any]:
    user = SimpleNamespace(id=uuid4())
    return cast(
        dict[str, Any], await _routes().update_matrix(w.matrix.id, request, user, w.db)
    )


@pytest.mark.asyncio
async def test_a_rename_alone_skips_the_release_snapshot(world: _World) -> None:
    response = await _patch(world, UpdateMatrixRequest.model_validate({"name": "New"}))

    assert world.resolved == ["resolve_project"]
    assert world.versions == [], "a rename appends no form version"
    assert response["name"] == "New" and response["columns_changed"] is False
    world.db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_column_change_takes_the_release_snapshot(world: _World) -> None:
    request = UpdateMatrixRequest.model_validate(
        {"columns": [{"name": "outcome", "description": "Primary outcome"}]}
    )

    response = await _patch(world, request)

    assert world.resolved == ["resolve_for_staling"]
    assert len(world.versions) == 1
    assert response["columns_changed"] is True
