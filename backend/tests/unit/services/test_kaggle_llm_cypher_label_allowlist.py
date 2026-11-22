"""LLM output must never become a Cypher label or type (GOO-639, IS2A-1).

``KaggleLLMBulkIngestionService._process_paper_with_llm`` built
``MERGE (e:Entity:{entity_type} ...)`` and ``MERGE (s)-[r:{rel_type}]->(t)``
from values the model extracted from a third-party abstract. A crafted
abstract could make the model emit Cypher there, run by a write-capable
account on the shared graph. Labels and relationship types now come only
from fixed allowlists; anything else is dropped.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple
from unittest.mock import AsyncMock

import pytest

from src.services.ingestion.kaggle_llm_bulk_ingestion import (
    KaggleLLMBulkIngestionService,
)

pytestmark = pytest.mark.unit

INJECTED = "CONCEPT {name: 'x'}) DETACH DELETE e //"


class _RecordingSession:
    def __init__(self) -> None:
        self.calls: List[Tuple[str, Dict[str, Any]]] = []

    async def run(self, query: str, params: Dict[str, Any] | None = None) -> None:
        self.calls.append((query, params or {}))


def _service(
    entities: List[Dict[str, Any]], relationships: List[Dict[str, Any]]
) -> KaggleLLMBulkIngestionService:
    svc = KaggleLLMBulkIngestionService(enable_embeddings=False)
    svc._extract_entities_with_llm = AsyncMock(return_value=entities)  # type: ignore[method-assign]
    svc._extract_relationships_with_llm = AsyncMock(return_value=relationships)  # type: ignore[method-assign]
    return svc


async def _run(svc: KaggleLLMBulkIngestionService) -> _RecordingSession:
    session = _RecordingSession()
    result = await svc._process_paper_with_llm(
        session, {"id": "p1", "title": "t", "abstract": "a", "categories": "cs.LG"}
    )
    assert result["success"], result
    return session


def _queries(
    session: _RecordingSession, marker: str
) -> List[Tuple[str, Dict[str, Any]]]:
    return [(q, p) for q, p in session.calls if marker in q]


@pytest.mark.asyncio
async def test_injected_entity_type_is_never_interpolated() -> None:
    session = await _run(
        _service(
            [
                {"name": "evil", "type": INJECTED},
                {"name": "BERT", "type": "model"},
                {"name": "Adam", "type": "optimization"},
            ],
            [],
        )
    )
    entity_writes = _queries(session, "MERGE (e:Entity")
    assert all("DETACH" not in q and "//" not in q for q, _ in entity_writes)
    assert [p["name"] for _, p in entity_writes] == ["BERT", "Adam"]
    assert "MERGE (e:Entity:MODEL {name: $name})" in entity_writes[0][0]
    assert "MERGE (e:Entity:OPTIMIZATION {name: $name})" in entity_writes[1][0]


@pytest.mark.asyncio
async def test_multiword_type_normalizes_onto_allowlist() -> None:
    session = await _run(_service([{"name": "CE", "type": "loss function"}], []))
    ((query, params),) = _queries(session, "MERGE (e:Entity")
    assert "MERGE (e:Entity:LOSS_FUNCTION {name: $name})" in query
    assert params["entity_type"] == "LOSS_FUNCTION"


@pytest.mark.asyncio
async def test_missing_entity_type_keeps_the_existing_concept_default() -> None:
    session = await _run(_service([{"name": "thing"}], []))
    ((query, _),) = _queries(session, "MERGE (e:Entity")
    assert "MERGE (e:Entity:CONCEPT {name: $name})" in query


@pytest.mark.asyncio
async def test_injected_relationship_type_is_dropped() -> None:
    session = await _run(
        _service(
            [{"name": "BERT", "type": "MODEL"}, {"name": "GLUE", "type": "DATASET"}],
            [
                {
                    "source": "BERT",
                    "target": "GLUE",
                    "relationship": "USES]->(t) DELETE t //",
                },
                {"source": "BERT", "target": "GLUE", "relationship": "evaluated on"},
                {"source": "BERT", "target": "GLUE"},
            ],
        )
    )
    rel_writes = [q for q, _ in _queries(session, "MERGE (s)-[r:")]
    assert len(rel_writes) == 2
    assert "MERGE (s)-[r:EVALUATED_ON]->(t)" in rel_writes[0]
    assert "MERGE (s)-[r:RELATED_TO]->(t)" in rel_writes[1]
    assert all("DELETE" not in q for q in rel_writes)
