"""Auxiliary LLM getters must pick up a rotated Azure credential (audit AG-1).

classifier, reflection, compactor, memory_store and tools_impl each pinned the
first client they built in a module global. The factories key their caches by
``credential_fingerprint()``, but the globals bypassed that, so a key rotated by
an Infisical sync kept being used until the process restarted. Their failures
are swallowed, so the agent quietly degraded.
"""

from __future__ import annotations

import itertools
from typing import Any, Callable, Iterator
from unittest.mock import MagicMock

import pytest

from src.services.agent import (
    classifier,
    compactor,
    graph,
    llm_factory,
    memory_store,
    reflection,
    tools_impl,
)

pytestmark = pytest.mark.unit

LIGHTWEIGHT_GETTERS: dict[str, Callable[[], Any]] = {
    "classifier": classifier._build_classifier_llm,
    "reflection": reflection._build_reflection_llm,
    "compactor": compactor._build_compactor_llm,
    "memory_store": memory_store._build_insights_llm,
}


@pytest.fixture
def fingerprint(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, str]]:
    current = {"value": "key-A"}
    counter = itertools.count()
    llm_factory.reset_llm_caches()
    monkeypatch.setattr(llm_factory, "credential_fingerprint", lambda: current["value"])
    monkeypatch.setattr(
        llm_factory,
        "_build_chat_llm",
        lambda *args, **kwargs: f"client-{next(counter)}",
    )
    yield current
    llm_factory.reset_llm_caches()


@pytest.mark.parametrize("name", sorted(LIGHTWEIGHT_GETTERS))
def test_lightweight_getter_rebuilds_after_rotation(
    name: str, fingerprint: dict[str, str]
) -> None:
    getter = LIGHTWEIGHT_GETTERS[name]

    first = getter()
    assert getter() is first  # still cached while the key is unchanged

    fingerprint["value"] = "key-B"
    rotated = getter()
    assert rotated != first


def test_tool_llm_asks_the_fingerprinted_builder_every_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = MagicMock(side_effect=["client-A", "client-B"])
    monkeypatch.setattr(graph, "_build_llm", builder)

    assert tools_impl._get_tool_llm() == "client-A"
    assert tools_impl._get_tool_llm() == "client-B"
    assert builder.call_count == 2


def test_no_module_level_llm_singletons_remain() -> None:
    for module, attr in (
        (classifier, "_CLASSIFIER_LLM"),
        (reflection, "_REFLECTION_LLM"),
        (compactor, "_COMPACTOR_LLM"),
        (memory_store, "_INSIGHTS_LLM"),
        (tools_impl, "_TOOL_LLM"),
    ):
        assert not hasattr(module, attr), f"{module.__name__}.{attr}"
