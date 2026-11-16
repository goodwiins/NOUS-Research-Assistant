"""GOO-473: a paper named by title in any lane is looked up on arXiv.

The title-to-arXiv chain lived only in the writing lane's instructions. The
shared rules and the search_documents zero-hit hint told the model to stop
when no local title matched, so a paper named in a research or general chat
was never fetched (the LangSmith "no title to ArXiv chain" finding).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.services.agent._prompts import SHARED_AGENT_RULES

pytestmark = pytest.mark.unit


def _named_source_section() -> str:
    start = SHARED_AGENT_RULES.index("## Named local source retrieval")
    end = SHARED_AGENT_RULES.index("\n## ", start + 1)
    return SHARED_AGENT_RULES[start:end]


def test_shared_rules_route_an_unmatched_paper_title_to_search_arxiv() -> None:
    section = _named_source_section()
    assert "search_arxiv" in section
    assert "ingest_arxiv_papers" in section
    # The local-source guarantees stay: no broad substitute, no metadata as
    # evidence.
    assert "never substitute broad retrieval" in section
    assert "title-search metadata as evidence" in section


async def test_zero_hit_hint_points_paper_titles_at_search_arxiv() -> None:
    from src.services.agent.tools_impl import _tool_search_documents

    scalars = MagicMock()
    scalars.all.return_value = []
    result_proxy = MagicMock()
    result_proxy.scalars.return_value = scalars
    db = SimpleNamespace(execute=AsyncMock(return_value=result_proxy))
    current_user = SimpleNamespace(organization_id="org-1")

    result = await _tool_search_documents(
        {"query": "Attention Is All You Need"}, db, cast(Any, current_user)
    )

    assert result["total"] == 0
    assert "on arXiv" in result["suggestion"]
    assert "do_kb_retrieve" in result["suggestion"]
    # search_documents is also bound to the data lane, which has no
    # search_arxiv, so the hint must not name that tool (see
    # test_recovery_suggestions_are_callable.py).
    assert "search_arxiv" not in result["suggestion"]
