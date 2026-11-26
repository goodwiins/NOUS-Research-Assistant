"""GOO-721: a source change in a Collection with no live release reads no
release graph (the PG suite in
``tests/integration/test_release_staling_lock_scope_postgres.py`` covers the
staling paths that do)."""

from typing import Any
from uuid import UUID, uuid4

import pytest

from src.services.research import draft_release_service
from src.services.research import extraction_forms_service as forms

pytestmark = pytest.mark.unit


async def _call(document_id: UUID | None) -> None:
    await forms._invalidate_releases(
        object(),  # type: ignore[arg-type]
        collection_id=uuid4(),
        changed=set(),
        actor_id=uuid4(),
        actor_role="adjudicator",
        event_id=None,
        kind="source_changed",
        document_id=document_id,
    )


@pytest.mark.asyncio
async def test_source_change_without_a_live_release_reads_no_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def no_live(_db: Any, _collection_id: UUID) -> list[UUID]:
        return []

    async def source_nodes(*_args: Any, **_kwargs: Any) -> set[Any]:
        calls.append("source_nodes")
        return set()

    async def invalidate(*_args: Any, **_kwargs: Any) -> list[UUID]:
        calls.append("invalidate")
        return []

    monkeypatch.setattr(draft_release_service, "_live_release_ids", no_live)
    monkeypatch.setattr(draft_release_service, "source_nodes", source_nodes)
    monkeypatch.setattr(draft_release_service, "invalidate_dependents", invalidate)

    await _call(uuid4())

    assert calls == []
