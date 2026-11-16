"""A fallback route records why the semantic classifier verdict was dropped.

GOO-472: an LLM classifier timeout or provider error silently fell back to
keywords, and the returned result (``source="keyword"``) looked exactly like a
keyword decision taken after a low-confidence LLM verdict. Nothing in state
or the trace said the route came from a failure.

Mutation check: drop the ``fallback_reason`` assignments in
``classify_intent_with_fallback`` or the ``intent_source`` /
``intent_fallback_reason`` keys in ``_classify_core`` / the preprocessing
defaults, and this file fails.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import HumanMessage

import src.services.agent._nodes_classify as nc
from src.core.config import settings
from src.services.agent.classifier import (
    ClassificationResult,
    classify_intent_with_fallback,
)

pytestmark = pytest.mark.unit

_LLM = "src.services.agent.classifier.classify_intent_llm"
# Two writing keywords: strong enough to survive the weak-keyword floor.
_WRITING_QUERY = "draft a literature review"
# The documented weak-keyword case: one incidental hit, demoted to general.
_WEAK_KEYWORD_QUERY = (
    'Create a research project named "Tool Coverage Study", then add '
    'the pre-loaded document titled "Seed Paper" to it, create a note '
    'in it titled "Kickoff", and finally list the project documents.'
)


class _NotFoundError(Exception):
    status_code = 404


@pytest.fixture(autouse=True)
def _azure_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "AGENT_INTENT_PROVIDER", "azure")


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (asyncio.TimeoutError(), "llm_timeout"),
        (RuntimeError("azure down"), "llm_error"),
        (_NotFoundError("no deployment"), "llm_deployment_not_found"),
    ],
)
async def test_provider_failure_is_recorded_on_the_keyword_result(
    error: BaseException, reason: str
) -> None:
    with (
        patch(_LLM, new_callable=AsyncMock, side_effect=error),
        patch(
            "src.services.agent.llm_factory.get_lightweight_model_name",
            return_value="classifier-deployment",
        ),
    ):
        result = await classify_intent_with_fallback(_WRITING_QUERY, {})

    assert result.intent == "writing"
    assert result.source == "keyword"
    assert result.fallback_reason == reason


async def test_weak_keyword_demotion_keeps_the_provider_failure_reason() -> None:
    with patch(_LLM, new_callable=AsyncMock, side_effect=asyncio.TimeoutError()):
        result = await classify_intent_with_fallback(_WEAK_KEYWORD_QUERY, {})

    assert result.intent == "general"
    assert result.source == "fallback"
    assert result.fallback_reason == "llm_timeout"


async def test_low_confidence_llm_is_recorded_as_such() -> None:
    weak = ClassificationResult(
        intent="general", confidence=0.3, reasoning="unsure", source="llm"
    )
    with patch(_LLM, new_callable=AsyncMock, return_value=weak):
        result = await classify_intent_with_fallback(_WRITING_QUERY, {})

    assert result.intent == "writing"
    assert result.source == "keyword"
    assert result.fallback_reason == "llm_low_confidence"


async def test_a_confident_llm_verdict_has_no_fallback_reason() -> None:
    confident = ClassificationResult(
        intent="writing", confidence=0.95, reasoning="drafting", source="llm"
    )
    with patch(_LLM, new_callable=AsyncMock, return_value=confident):
        result = await classify_intent_with_fallback(_WRITING_QUERY, {})

    assert result.source == "llm"
    assert result.fallback_reason == ""


async def test_classify_core_writes_source_and_reason_to_state() -> None:
    fallback = ClassificationResult(
        intent="writing",
        confidence=0.5,
        reasoning="keywords",
        source="keyword",
        fallback_reason="llm_timeout",
    )
    state: Any = {"messages": [HumanMessage(content=_WRITING_QUERY)]}
    with patch(
        "src.services.agent.classifier.classify_intent_with_fallback",
        new_callable=AsyncMock,
        return_value=fallback,
    ):
        update = await nc._classify_core(state, {})

    assert update["intent"] == "writing"
    assert update["intent_source"] == "keyword"
    assert update["intent_fallback_reason"] == "llm_timeout"


async def test_a_crashed_classify_subtask_is_marked_as_an_error() -> None:
    async def rag(_s: Any, _c: Any) -> dict[str, Any]:
        return {"retrieved_contexts": []}

    async def classify(_s: Any, _c: Any) -> dict[str, Any]:
        raise RuntimeError("classifier exploded")

    async def memory(_s: Any, _c: Any) -> dict[str, Any]:
        return {"user_memories": []}

    with (
        patch.object(nc, "rag_node", rag),
        patch.object(nc, "_classify_core", classify),
        patch.object(nc, "memory_retrieval_node", memory),
        patch.object(nc, "tag_trace_intent", lambda *_a, **_k: None),
    ):
        state: Any = {}
        result = await nc.preprocessing_node(state, {})

    assert result["intent"] == "general"
    assert result["intent_source"] == "error"
    assert result["intent_fallback_reason"] == "classifier_exception"
