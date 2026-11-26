"""GOO-713: model-supplied count arguments and the external-search fan-out.

* Five handlers capped limits with ``min(args.get("limit", N), M)`` on raw
  model output, so a string such as ``"10"`` raised TypeError and failed an
  otherwise valid call. They now go through ``coerce_int``.
* An unscoped ``search_external_database`` queried every connector with no
  concurrency cap and no overall deadline, and the outer 30s retry re-ran the
  whole fan-out. It is now bounded, returns marked partial results on the
  deadline, and is tagged NO_OUTER_RETRY.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.services.agent import tools_impl
from src.services.agent.tool_registry import ToolPolicyTag
from src.services.agent.tools_impl import coerce_int
from src.services.connectors.base import (
    ConnectorCapability,
    ConnectorDomain,
    ConnectorInfo,
    ConnectorResult,
    ExternalDBConnector,
)

pytestmark = [pytest.mark.unit]


# ---------------------------------------------------------------------------
# coerce_int
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (10, 10),
        ("10", 10),
        (" 7 ", 7),
        (3.0, 3),
        (None, 20),
        ("abc", 20),
        ("ten", 20),
        ("3.7", 20),
        ("", 20),
        (3.7, 20),
        (float("nan"), 20),
        (float("inf"), 20),
        (True, 20),
        (False, 20),
        ([5], 20),
        ({"n": 5}, 20),
        (-5, 1),
        ("-5", 1),
        (0, 1),
        (500, 50),
        ("500", 50),
        (1e9, 50),
    ],
)
def test_coerce_int(value: Any, expected: int) -> None:
    assert coerce_int(value, 20, 1, 50) == expected


def test_coerce_int_clamps_default_too() -> None:
    assert coerce_int(None, 99, 1, 50) == 50


# ---------------------------------------------------------------------------
# The five handler sites survive "10", "abc" and None
# ---------------------------------------------------------------------------

# raw value -> the limit the handler must pass on, given its own default.
_CASES = [("10", 10), ("abc", None), (None, None)]


class _Captured(Exception):
    def __init__(self, value: Any) -> None:
        super().__init__(value)
        self.value = value


def _user() -> Any:
    return cast(Any, SimpleNamespace(organization_id="org-1", id="u-1"))


# arXiv caps at _ARXIV_TOOL_MAX_RESULTS (5), so its numeric string is "3".
@pytest.mark.parametrize(("raw", "expected"), [("3", 3), ("abc", None), (None, None)])
async def test_search_arxiv_max_results(raw: Any, expected: Optional[int]) -> None:
    def _stop(query: Any, max_results: int, *rest: Any) -> Any:
        raise _Captured(max_results)

    with patch.object(tools_impl, "_arxiv_cache_key", side_effect=_stop):
        with pytest.raises(_Captured) as exc:
            await tools_impl._search_arxiv_once(
                {"query": "graph neural networks", "max_results": raw}
            )
    assert exc.value.value == (expected if expected is not None else 5)


@pytest.mark.parametrize(("raw", "expected"), _CASES)
async def test_search_documents_max_results(raw: Any, expected: Optional[int]) -> None:
    scalars = MagicMock()
    scalars.all.return_value = []
    proxy = MagicMock()
    proxy.scalars.return_value = scalars
    db = SimpleNamespace(execute=AsyncMock(return_value=proxy))

    result = await tools_impl._tool_search_documents(
        {"query": "transformers", "max_results": raw}, cast(Any, db), _user()
    )

    assert "error" not in result
    stmt = db.execute.await_args.args[0]
    assert stmt._limit == (expected if expected is not None else 10)


@pytest.mark.parametrize(("raw", "expected"), _CASES)
async def test_search_knowledge_graph_limit(raw: Any, expected: Optional[int]) -> None:
    from src.services.knowledge_graph.knowledge_graph_service import (
        knowledge_graph_service,
    )

    seen: Dict[str, Any] = {}

    def _search(**kwargs: Any) -> List[Any]:
        seen.update(kwargs)
        return []

    with patch.object(knowledge_graph_service, "search_entities", side_effect=_search):
        result = await tools_impl._tool_search_knowledge_graph(
            {"query": "attention", "limit": raw}, _user()
        )

    assert "error" not in result
    assert seen["limit"] == (expected if expected is not None else 20)


@pytest.mark.parametrize(("raw", "expected"), _CASES)
async def test_explore_entity_neighborhood_limit(
    raw: Any, expected: Optional[int]
) -> None:
    from src.services.knowledge_graph.knowledge_graph_service import (
        knowledge_graph_service,
    )

    seen: Dict[str, Any] = {}

    def _neighborhood(**kwargs: Any) -> Dict[str, Any]:
        seen.update(kwargs)
        return {"entities": [], "relationships": []}

    with patch.object(
        knowledge_graph_service, "get_neighborhood", side_effect=_neighborhood
    ):
        result = await tools_impl._tool_explore_entity_neighborhood(
            {"entity_id": "e-1", "limit": raw}, _user()
        )

    assert "error" not in result
    assert seen["limit"] == (expected if expected is not None else 30)


@pytest.mark.parametrize(("raw", "expected"), _CASES)
async def test_search_external_database_max_results(
    raw: Any, expected: Optional[int]
) -> None:
    conn = _FakeConn("goo713_one")
    with patch(
        "src.services.connectors.connector_registry.list_available",
        return_value=[conn],
    ):
        result = await tools_impl._tool_search_external_database(
            {"query": "q", "max_results": raw}
        )

    assert "error" not in result
    assert conn.seen_max_results == [expected if expected is not None else 5]


# ---------------------------------------------------------------------------
# search_external_database fan-out bounds
# ---------------------------------------------------------------------------


def _info(name: str) -> ConnectorInfo:
    return ConnectorInfo(
        name=name,
        display_name=name,
        description="test connector",
        domains=[ConnectorDomain.GENERAL],
        capabilities=[ConnectorCapability.SEARCH],
        base_url="https://example.invalid",
    )


class _FakeConn(ExternalDBConnector):
    def __init__(
        self,
        name: str,
        *,
        delay: float = 0.0,
        tracker: Optional[Dict[str, int]] = None,
    ) -> None:
        self._name = name
        self._delay = delay
        self._tracker = tracker
        self.seen_max_results: List[int] = []

    @property
    def info(self) -> ConnectorInfo:
        return _info(self._name)

    async def search(
        self,
        query: str,
        max_results: int = 10,
        filters: Optional[Dict[str, Any]] = None,
    ) -> List[ConnectorResult]:
        del filters
        self.seen_max_results.append(max_results)
        tracker = self._tracker
        if tracker is not None:
            tracker["now"] += 1
            tracker["peak"] = max(tracker["peak"], tracker["now"])
        try:
            await asyncio.sleep(self._delay)
        finally:
            if tracker is not None:
                tracker["now"] -= 1
        return [
            ConnectorResult(
                id=f"{self._name}-1",
                title=query,
                source=self._name,
                url="https://example.invalid",
            )
        ]


async def _search_unscoped(connectors: List[ExternalDBConnector]) -> Dict[str, Any]:
    with patch(
        "src.services.connectors.connector_registry.list_available",
        return_value=connectors,
    ):
        result: Dict[str, Any] = await tools_impl._tool_search_external_database(
            {"query": "q"}
        )
    return result


async def test_fanout_never_exceeds_concurrency_cap() -> None:
    cap = tools_impl._EXTERNAL_SEARCH_CONCURRENCY
    tracker = {"now": 0, "peak": 0}
    connectors: List[ExternalDBConnector] = [
        _FakeConn(f"goo713_c{i}", delay=0.02, tracker=tracker) for i in range(cap * 3)
    ]

    result = await _search_unscoped(connectors)

    assert "error" not in result
    assert result["total_results"] == cap * 3
    assert "partial" not in result
    assert tracker["peak"] <= cap
    assert tracker["peak"] == cap  # the cap is used, not serialised to one


async def test_deadline_returns_partial_results_marked_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tools_impl, "_EXTERNAL_SEARCH_TIMEOUT_SECONDS", 0.2)
    fast = _FakeConn("goo713_fast")
    slow = _FakeConn("goo713_slow", delay=5.0)

    result = await _search_unscoped([fast, slow])

    assert "error" not in result
    assert result["partial"] is True
    assert result["unfinished_connectors"] == ["goo713_slow"]
    assert result["connectors_searched"] == ["goo713_fast"]
    assert result["total_results"] == 1


async def test_deadline_with_nothing_finished_is_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tools_impl, "_EXTERNAL_SEARCH_TIMEOUT_SECONDS", 0.2)

    result = await _search_unscoped([_FakeConn("goo713_slow", delay=5.0)])

    assert "error" in result
    assert result["unfinished_connectors"] == ["goo713_slow"]
    assert "results" not in result


def test_deadline_is_under_the_tool_timeout() -> None:
    from src.services.agent._nodes_tools import TOOL_TIMEOUT_SECONDS

    assert 0 < tools_impl._EXTERNAL_SEARCH_TIMEOUT_SECONDS < TOOL_TIMEOUT_SECONDS


def test_search_external_database_has_no_outer_retry() -> None:
    from src.services.agent._nodes_tools import _NO_OUTER_RETRY_TOOLS
    from src.services.agent.tools import TOOL_REGISTRY

    assert TOOL_REGISTRY.has_policy(
        "search_external_database", ToolPolicyTag.NO_OUTER_RETRY
    )
    assert "search_external_database" in _NO_OUTER_RETRY_TOOLS


class _HangingConn(_FakeConn):
    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.cancelled = False

    async def search(
        self,
        query: str,
        max_results: int = 10,
        filters: Optional[Dict[str, Any]] = None,
    ) -> List[ConnectorResult]:
        try:
            await asyncio.sleep(5.0)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return []


async def test_cancelling_the_call_cancels_connector_searches() -> None:
    conn = _HangingConn("goo713_hang")
    call = asyncio.ensure_future(_search_unscoped([conn]))
    await asyncio.sleep(0.05)
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call
    await asyncio.sleep(0)
    assert conn.cancelled, "an orphaned connector search kept running"


# ---------------------------------------------------------------------------
# The remaining sites: max_depth (x2) and do_kb_retrieve top_k
# ---------------------------------------------------------------------------

# raw value -> expected, as (raw, kind) where kind picks default / lo / hi.
_BAD_INPUTS = [("abc", "default"), (True, "default"), (-4, "lo"), (999, "hi")]


def _expect(kind: str, default: int, hi: int) -> int:
    return {"default": default, "lo": 1, "hi": hi}[kind]


@pytest.mark.parametrize(("raw", "kind"), _BAD_INPUTS)
async def test_explore_entity_neighborhood_max_depth(raw: Any, kind: str) -> None:
    from src.services.knowledge_graph.knowledge_graph_service import (
        knowledge_graph_service,
    )

    seen: Dict[str, Any] = {}

    def _neighborhood(**kwargs: Any) -> Dict[str, Any]:
        seen.update(kwargs)
        return {"entities": [], "relationships": []}

    with patch.object(
        knowledge_graph_service, "get_neighborhood", side_effect=_neighborhood
    ):
        result = await tools_impl._tool_explore_entity_neighborhood(
            {"entity_id": "e-1", "max_depth": raw}, _user()
        )

    assert "error" not in result
    assert seen["max_depth"] == _expect(kind, 2, 3)
    assert type(seen["max_depth"]) is int


@pytest.mark.parametrize(("raw", "kind"), _BAD_INPUTS)
async def test_find_entity_paths_max_depth(raw: Any, kind: str) -> None:
    from src.services.knowledge_graph.knowledge_graph_service import (
        knowledge_graph_service,
    )

    seen: Dict[str, Any] = {}

    def _paths(**kwargs: Any) -> List[Any]:
        seen.update(kwargs)
        return []

    with patch.object(knowledge_graph_service, "find_paths", side_effect=_paths):
        result = await tools_impl._tool_find_entity_paths(
            {
                "source_entity_id": "e-1",
                "target_entity_id": "e-2",
                "max_depth": raw,
            },
            _user(),
        )

    assert "error" not in result
    assert seen["max_depth"] == _expect(kind, 3, 5)
    assert type(seen["max_depth"]) is int


@pytest.mark.parametrize(("raw", "kind"), _BAD_INPUTS)
async def test_do_kb_retrieve_top_k(raw: Any, kind: str) -> None:
    kb_settings = SimpleNamespace(
        DO_KB_ENABLED=False, BEDROCK_KB_ID="", DO_KB_DEFAULT_TOP_K=8
    )
    local = AsyncMock(return_value={"chunks": [], "total": 0})

    with (
        patch("src.core.config.settings", kb_settings),
        patch.object(tools_impl, "_local_unscoped_fulltext_retrieve", local),
    ):
        result = await tools_impl._tool_do_kb_retrieve(
            {"query": "attention", "top_k": raw}, None, _user()
        )

    assert "error" not in result
    assert local.await_args is not None
    top_k = local.await_args.kwargs["top_k"]
    assert top_k == _expect(kind, 8, 20)
    assert type(top_k) is int
