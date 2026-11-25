"""Org-scoped connector builder shared by research-run routes and Celery tasks.

Moved verbatim from ``src.api.research_engine.runs`` (GOO-503): the scheduled
search update task (``src/tasks/search_update_tasks.py``) builds the same
tenant-scoped connectors as the run routes, so the builder must live where
tasks can import it without inverting the dependency direction (services and
tasks must never import ``src.api`` — enforced by
``tests/unit/test_import_direction.py``). ``runs.py`` re-exports both
functions under their old private names (``_search_rag_store``,
``_build_connectors``), so existing imports and monkeypatch targets keep
working.
"""

import asyncio
import functools
from typing import Any, Dict, Optional

from src.services.research_engine.connectors.registry import build_connectors


async def search_rag_store(
    query: str, max_results: int = 50, *, organization_id: Optional[str] = None
) -> Dict[str, Any]:
    """Search the existing hybrid RAG index for local-store style connector output.

    MUST be scoped to the run owner's ``organization_id`` — without it
    ``hybrid_search_service.search`` runs org-unfiltered and a research run
    would surface (and copy ``full_text`` from) every tenant's documents.
    """
    if not organization_id:
        return {"results": []}

    from src.models.search_schemas import SearchQuery, SearchSortOrder, SearchType
    from src.services.search.hybrid_search_service import hybrid_search_service

    loop = asyncio.get_running_loop()
    response = await loop.run_in_executor(
        None,
        lambda: hybrid_search_service.search(
            SearchQuery(
                query=query,
                limit=max_results,
                search_type=SearchType.HYBRID,
                sort_order=SearchSortOrder.RELEVANCE,
                filters=None,
            ),
            organization_id=organization_id,
        ),
    )

    results = []
    for item in response.results:
        metadata = getattr(item, "metadata", {}) or {}
        content = (
            metadata.get("full_text")
            or metadata.get("text")
            or getattr(item, "content_preview", None)
            or getattr(item, "content_snippet", None)
            or ""
        )
        results.append(
            {
                "id": str(getattr(item, "document_id", "")),
                "title": getattr(item, "title", ""),
                "content": content,
                "metadata": metadata,
            }
        )
    return {"results": results}


def build_run_connectors(organization_id: Optional[str] = None) -> Dict[str, Any]:
    """Create connector instances for workflow execution.

    ``organization_id`` (the run owner's) is bound into the rag_store search so
    the local-index connector only ever returns this tenant's documents.
    """
    rag_search = functools.partial(search_rag_store, organization_id=organization_id)
    return build_connectors(rag_search)
