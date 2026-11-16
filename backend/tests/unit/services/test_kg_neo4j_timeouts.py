"""GOO-459: Neo4j driver + per-query timeouts, and timeouts open the breaker.

Before: the shared driver was built with no connection / acquisition timeout,
every query ran without a transaction timeout, and ``get_session`` only fed
``ServiceUnavailable`` / ``SessionExpired`` to the circuit breaker. A hung
Neo4j therefore pinned threadpool workers and never opened the breaker.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from neo4j import Query
from neo4j.exceptions import (
    ConnectionAcquisitionTimeoutError,
    CypherSyntaxError,
    Neo4jError,
)

from src.services.knowledge_graph import knowledge_graph_service as kg_module
from src.services.knowledge_graph.knowledge_graph_service import KnowledgeGraphService

pytestmark = pytest.mark.unit


@pytest.fixture
def breaker(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    b = MagicMock()
    b.can_execute.return_value = True
    monkeypatch.setattr(kg_module, "get_circuit_breaker", lambda _name: b)
    return b


@pytest.fixture
def svc(monkeypatch: pytest.MonkeyPatch) -> KnowledgeGraphService:
    monkeypatch.setattr(KnowledgeGraphService, "_schema_ensured", True)
    s = KnowledgeGraphService()
    s.driver = MagicMock()
    return s


def test_driver_is_built_with_connection_and_acquisition_timeouts(
    monkeypatch: pytest.MonkeyPatch, breaker: MagicMock
) -> None:
    monkeypatch.setattr(KnowledgeGraphService, "_driver_instance", None)
    monkeypatch.setattr(KnowledgeGraphService, "_schema_ensured", True)
    monkeypatch.setattr(kg_module.settings, "NEO4J_CONNECTION_TIMEOUT_SECONDS", 7.0)
    monkeypatch.setattr(
        kg_module.settings, "NEO4J_CONNECTION_ACQUISITION_TIMEOUT_SECONDS", 9.0
    )
    driver_factory = MagicMock()
    monkeypatch.setattr(kg_module.GraphDatabase, "driver", driver_factory)

    KnowledgeGraphService()._connect()

    kwargs = driver_factory.call_args.kwargs
    assert kwargs["connection_timeout"] == 7.0
    assert kwargs["connection_acquisition_timeout"] == 9.0


def test_session_run_applies_query_timeout(
    monkeypatch: pytest.MonkeyPatch, svc: KnowledgeGraphService, breaker: MagicMock
) -> None:
    monkeypatch.setattr(kg_module.settings, "NEO4J_QUERY_TIMEOUT_SECONDS", 12.0)
    assert isinstance(svc.driver, MagicMock)
    raw = svc.driver.session.return_value

    session: Any
    with svc.get_session() as session:
        session.run("MATCH (e:Entity) RETURN e", {"a": 1})

    query, params = raw.run.call_args.args
    assert isinstance(query, Query)
    assert query.text == "MATCH (e:Entity) RETURN e"
    assert query.timeout == 12.0
    assert params == {"a": 1}


def test_explicit_transaction_gets_query_timeout(
    monkeypatch: pytest.MonkeyPatch, svc: KnowledgeGraphService, breaker: MagicMock
) -> None:
    monkeypatch.setattr(kg_module.settings, "NEO4J_QUERY_TIMEOUT_SECONDS", 12.0)
    assert isinstance(svc.driver, MagicMock)
    raw = svc.driver.session.return_value

    session: Any
    with svc.get_session() as session:
        session.begin_transaction()

    assert raw.begin_transaction.call_args.kwargs["timeout"] == 12.0


@pytest.mark.parametrize(
    "error",
    [
        Neo4jError._hydrate_neo4j(
            code="Neo.ClientError.Transaction.TransactionTimedOutClientConfiguration",
            message="timed out",
        ),
        Neo4jError._hydrate_neo4j(
            code="Neo.TransientError.Transaction.TransactionTimedOut",
            message="timed out",
        ),
        ConnectionAcquisitionTimeoutError("pool exhausted"),
        TimeoutError("socket read timed out"),
    ],
)
def test_timeouts_count_toward_the_breaker(
    svc: KnowledgeGraphService, breaker: MagicMock, error: Exception
) -> None:
    with pytest.raises(type(error)):
        with svc.get_session():
            raise error

    breaker.record_failure.assert_called_once_with(error)
    breaker.record_success.assert_not_called()


def test_query_bugs_do_not_trip_the_breaker(
    svc: KnowledgeGraphService, breaker: MagicMock
) -> None:
    error = CypherSyntaxError("bad cypher")
    with pytest.raises(CypherSyntaxError):
        with svc.get_session():
            raise error

    breaker.record_failure.assert_not_called()
