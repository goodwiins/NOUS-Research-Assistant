"""GOO-495: document-scoped KG reads match on ``document_ids`` membership.

:Entity nodes are shared by every document in an org that names them, but
``source_document_id`` records only the FIRST creator. Project-/document-
scoped reads filtered ``e.source_document_id IN $source_document_ids``, so an
entity first extracted from document A vanished from a project holding only
document B, even though B's ingestion appended B to ``e.document_ids`` (KN2).
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator
from unittest.mock import MagicMock

import pytest

from src.services.knowledge_graph.knowledge_graph_service import (
    KnowledgeGraphService,
    _entity_scope_predicate,
    _two_endpoint_scope,
)

pytestmark = pytest.mark.unit

MEMBERSHIP = (
    "any(doc_id IN coalesce({a}.document_ids, []) WHERE doc_id IN $source_document_ids)"
)


def _svc_with_session() -> tuple:
    svc = KnowledgeGraphService()
    session = MagicMock()
    session.run.return_value = []

    @contextmanager
    def _get_session(*_a: Any, **_k: Any) -> Iterator[MagicMock]:
        yield session

    svc.get_session = _get_session  # type: ignore[method-assign]
    return svc, session


def test_entity_scope_matches_shared_document_membership() -> None:
    params: dict = {}
    pred = _entity_scope_predicate("e", ["doc-B"], None, params)

    assert MEMBERSHIP.format(a="e") in pred
    # Legacy nodes without document_ids still match on the creator stamp.
    assert "e.source_document_id IN $source_document_ids" in pred
    assert params == {"source_document_ids": ["doc-B"]}


def test_two_endpoint_scope_matches_membership_on_both_endpoints() -> None:
    frag, _ = _two_endpoint_scope("source", "target", ["doc-B"], None)

    assert MEMBERSHIP.format(a="source") in frag
    assert MEMBERSHIP.format(a="target") in frag


@pytest.mark.parametrize(
    "call",
    [
        lambda s: s.get_relationships("e1", source_document_ids=["doc-B"]),
        lambda s: s.get_relationships_among(
            ["e1", "e2"], source_document_ids=["doc-B"]
        ),
        lambda s: s.get_relationships_for_entities(
            ["e1"], source_document_ids=["doc-B"]
        ),
    ],
    ids=["get_relationships", "among", "for_entities"],
)
def test_project_scoped_relationship_reads_use_membership(call: Any) -> None:
    svc, session = _svc_with_session()

    call(svc)

    query = session.run.call_args.args[0]
    assert MEMBERSHIP.format(a="source") in query
    assert MEMBERSHIP.format(a="target") in query


def test_project_scoped_analytics_use_membership() -> None:
    svc, session = _svc_with_session()

    svc.get_graph_analytics(source_document_ids=["doc-B"], organization_id="org-1")

    entity_query = session.run.call_args_list[0].args[0]
    assert MEMBERSHIP.format(a="e") in entity_query
    assert "e.organization_id = $organization_id" in entity_query
