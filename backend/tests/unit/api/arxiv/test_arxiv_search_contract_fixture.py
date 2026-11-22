"""GOO-670: pin POST /api/v1/arxiv/search's shape to the Trigger.dev fixture.

The bulk-ingest Trigger.dev task parses ``tests/trigger/fixtures/
arxiv-search-response.json`` (see tests/trigger/arxiv-search-contract.test.mjs).
That fixture must stay exactly what this route serializes, so a change to the
route's response model fails here instead of silently ingesting zero papers.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, List

import pytest
from fastapi.routing import APIRoute
from pydantic import TypeAdapter

from src.api.arxiv.core import ArXivPaperResponse, router

pytestmark = pytest.mark.unit

FIXTURE = (
    Path(__file__).resolve().parents[5]
    / "tests"
    / "trigger"
    / "fixtures"
    / "arxiv-search-response.json"
)


def _search_route() -> APIRoute:
    return next(
        route
        for route in router.routes
        if isinstance(route, APIRoute)
        and route.path.endswith("/search")
        and "POST" in route.methods
    )


def test_search_route_returns_a_bare_list_of_papers() -> None:
    assert _search_route().response_model == List[ArXivPaperResponse]


def test_trigger_fixture_round_trips_through_the_route_response_model() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert isinstance(raw, list) and raw, "fixture must be a non-empty JSON array"
    adapter: TypeAdapter[Any] = TypeAdapter(_search_route().response_model)
    serialized = json.loads(adapter.dump_json(adapter.validate_python(raw)))
    assert serialized == raw
