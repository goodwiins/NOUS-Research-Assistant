"""GOO-695: a failed-extraction retry rebuilds Postgres too, and gives up.

GOO-688 marked a document whose ingestion-time entity extraction failed as
``neo4j_index_status=failed`` so the reconciler retries it. But the retry
only rebuilt Neo4j: the Postgres ``Entity`` rows kept the previous run's set
while the status read "completed". And a document that always fails was
retried every 30 minutes forever.
"""

import uuid
from datetime import datetime, timezone
from typing import Any, List, Optional, Tuple
from unittest.mock import MagicMock

import pytest

from src.models.document import Document
from src.models.entity import Entity, EntityType, ExtractionMethod
from src.services.knowledge_graph.repair import GraphRepairOutcome
from src.tasks import reconcile_tasks as rt
from tests.unit.tasks.test_reconcile_tasks import (  # noqa: F401 - fixture
    COMPLETED,
    FAILED,
    _get,
    _run,
    _seed_doc,
    _settings,
    session_factory,
)

pytestmark = pytest.mark.unit

ABANDONED = "abandoned"


def _entity(name: str, method: ExtractionMethod, doc_id: Any, org_id: Any) -> Entity:
    return Entity(
        entity_type=EntityType.PERSON,
        name=name,
        extraction_method=method,
        extracted_at=datetime.now(timezone.utc),
        confidence=0.9,
        document_id=doc_id,
        organization_id=org_id,
    )


def _seed_stale(
    factory: Any, *, stale: bool = True, attempts: int = 0
) -> Tuple[Any, Any]:
    org_id = uuid.uuid4()
    doc_id = _seed_doc(factory, org_id=org_id, neo4j=FAILED)
    with factory() as db:
        doc = db.query(Document).filter(Document.id == doc_id).one()
        doc.document_metadata = {
            rt.GRAPH_REDRIVE_KEY: {
                "attempts": attempts,
                "postgres_entities_stale": stale,
            }
        }
        db.add(_entity("Old Spacy Name", ExtractionMethod.SPACY, doc_id, org_id))
        db.add(_entity("Curated Name", ExtractionMethod.MANUAL, doc_id, org_id))
        db.commit()
    return doc_id, org_id


def _names(factory: Any, doc_id: Any) -> List[str]:
    with factory() as db:
        return sorted(
            e.name for e in db.query(Entity).filter(Entity.document_id == doc_id)
        )


def _redrive_state(factory: Any, doc_id: Any) -> Any:
    return (_get(factory, doc_id).document_metadata or {}).get(rt.GRAPH_REDRIVE_KEY)


def _ok_with(doc: Any, *names: str) -> GraphRepairOutcome:
    return GraphRepairOutcome(
        document_id=str(doc.id),
        entities_found=len(names),
        entities_created=len(names),
        extracted_entities=tuple(
            _entity(n, ExtractionMethod.SPACY, doc.id, doc.organization_id)
            for n in names
        ),
    )


def test_retry_rebuilds_stale_postgres_entities(session_factory: Any) -> None:
    doc_id, _ = _seed_stale(session_factory)
    repair = MagicMock(side_effect=lambda doc, **kw: _ok_with(doc, "Ada Lovelace"))

    result, _ = _run(session_factory, _settings(apply=True), repair=repair)

    assert result["kg_repaired"] == 1
    assert _get(session_factory, doc_id).neo4j_index_status == COMPLETED
    # The pipeline's own rows are replaced; curated (MANUAL) rows are kept.
    assert _names(session_factory, doc_id) == ["Ada Lovelace", "Curated Name"]
    assert _redrive_state(session_factory, doc_id) is None


def test_retry_leaves_fresh_postgres_entities_alone(session_factory: Any) -> None:
    doc_id, _ = _seed_stale(session_factory, stale=False)
    repair = MagicMock(side_effect=lambda doc, **kw: _ok_with(doc, "Ada Lovelace"))

    _run(session_factory, _settings(apply=True), repair=repair)

    # A plain Neo4j outage: Postgres was written at ingestion and is current.
    assert _names(session_factory, doc_id) == ["Curated Name", "Old Spacy Name"]


def test_postgres_is_rebuilt_even_when_the_graph_write_fails(
    session_factory: Any,
) -> None:
    doc_id, _ = _seed_stale(session_factory)

    def _graph_down(doc: Any, **kw: Any) -> GraphRepairOutcome:
        ok = _ok_with(doc, "Ada Lovelace")
        return GraphRepairOutcome(
            document_id=ok.document_id,
            error_message="neo4j down",
            extracted_entities=ok.extracted_entities,
        )

    _run(
        session_factory,
        _settings(apply=True),
        repair=MagicMock(side_effect=_graph_down),
    )

    assert _names(session_factory, doc_id) == ["Ada Lovelace", "Curated Name"]
    state = _redrive_state(session_factory, doc_id)
    assert state["postgres_entities_stale"] is False
    assert state["attempts"] == 1
    assert _get(session_factory, doc_id).neo4j_index_status == FAILED


def test_a_document_that_always_fails_stops_after_max_attempts(
    session_factory: Any,
) -> None:
    doc_id, _ = _seed_stale(session_factory)
    repair = MagicMock(
        side_effect=lambda doc, **kw: GraphRepairOutcome(
            document_id=str(doc.id), error_message="extraction failed"
        )
    )
    settings = _settings(apply=True)
    settings.RECONCILER_NEO4J_MAX_ATTEMPTS = 3

    for _ in range(5):
        _run(session_factory, settings, repair=repair)

    assert repair.call_count == 3, "kept retrying past the cap"
    doc = _get(session_factory, doc_id)
    assert doc.neo4j_index_status == ABANDONED
    assert _redrive_state(session_factory, doc_id)["attempts"] == 3
    # Extraction never succeeded, so the previous entities are still there.
    assert _names(session_factory, doc_id) == ["Curated Name", "Old Spacy Name"]


def test_success_resets_the_attempt_count(session_factory: Any) -> None:
    doc_id, _ = _seed_stale(session_factory, stale=False, attempts=2)
    repair = MagicMock(side_effect=lambda doc, **kw: _ok_with(doc, "Ada Lovelace"))

    _run(session_factory, _settings(apply=True), repair=repair)

    assert _get(session_factory, doc_id).neo4j_index_status == COMPLETED
    assert _redrive_state(session_factory, doc_id) is None


def test_default_cap_is_five(session_factory: Any) -> None:
    doc_id, _ = _seed_stale(session_factory, attempts=4)
    repair = MagicMock(
        side_effect=lambda doc, **kw: GraphRepairOutcome(
            document_id=str(doc.id), error_message="still failing"
        )
    )
    settings = _settings(apply=True)  # no RECONCILER_NEO4J_MAX_ATTEMPTS

    _run(session_factory, settings, repair=repair)

    assert _get(session_factory, doc_id).neo4j_index_status == ABANDONED
