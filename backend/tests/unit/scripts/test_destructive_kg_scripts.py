"""Guards for destructive knowledge-graph maintenance scripts (TS-H, GOO-630).

rebuild_kg_llm.py used to delete every organization's graph before it looked
at its source, with no confirmation, and exit 0 when the source was empty.
cleanup_entities.py deleted every manual entity on invocation and exited 0 on
error. cleanup_garbage_entities.py --aggressive deleted Meta, Yale, Duke...
because its whitelist was never matched.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any, List, Tuple

import pytest

pytestmark = pytest.mark.unit

BACKEND_ROOT = Path(__file__).resolve().parents[3]
REPO_ROOT = BACKEND_ROOT.parent


def _load(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def rebuild() -> Any:
    return _load(
        BACKEND_ROOT / "scripts" / "maintenance" / "rebuild_kg_llm.py",
        "rebuild_kg_llm_under_test",
    )


class Calls:
    def __init__(
        self, entities: int = 5, probe_error: str = "", failed_chunks: int = 0
    ) -> None:
        self.events: List[str] = []
        self.entities = entities
        self.probe_error = probe_error
        self.failed_chunks = failed_chunks

    def probe(self) -> None:
        self.events.append("probe")
        if self.probe_error:
            raise RuntimeError(self.probe_error)

    def wipe(self) -> None:
        self.events.append("wipe")

    def rebuild(self, docs: list[dict]) -> Tuple[int, int]:
        self.events.append(f"rebuild:{len(docs)}")
        return self.entities, self.failed_chunks


DOCS = [{"id": "d1", "title": "t", "filename": "f", "content_text": "x"}]


def test_rebuild_requires_explicit_database_url(
    rebuild: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("KG_DATABASE_URL", raising=False)
    calls = Calls()
    code = rebuild.run(
        ["--yes"],
        load=lambda _url: DOCS,
        wipe=calls.wipe,
        rebuild=calls.rebuild,
        probe=calls.probe,
    )
    assert code != 0
    assert calls.events == []


def test_rebuild_default_is_a_dry_run(
    rebuild: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KG_DATABASE_URL", "postgresql://scratch/db")
    calls = Calls()
    code = rebuild.run(
        [],
        load=lambda _url: DOCS,
        wipe=calls.wipe,
        rebuild=calls.rebuild,
        probe=calls.probe,
    )
    assert code == 0
    assert calls.events == []


def test_rebuild_refuses_to_wipe_when_source_is_empty(
    rebuild: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KG_DATABASE_URL", "postgresql://scratch/db")
    calls = Calls()
    code = rebuild.run(
        ["--yes"],
        load=lambda _url: [],
        wipe=calls.wipe,
        rebuild=calls.rebuild,
        probe=calls.probe,
    )
    assert code != 0
    assert calls.events == []


def test_rebuild_refuses_to_wipe_when_source_fails(
    rebuild: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KG_DATABASE_URL", "postgresql://scratch/db")
    calls = Calls()

    def broken(_url: str) -> list[dict]:
        raise RuntimeError("connection refused")

    code = rebuild.run(
        ["--yes"],
        load=broken,
        wipe=calls.wipe,
        rebuild=calls.rebuild,
        probe=calls.probe,
    )
    assert code != 0
    assert calls.events == []


@pytest.fixture(scope="module")
def garbage() -> Any:
    return _load(
        BACKEND_ROOT / "examples" / "cleanup_garbage_entities.py",
        "cleanup_garbage_entities_under_test",
    )


@pytest.mark.parametrize("name", ["Meta", "Yale", "Duke", "Uber", "Rice", "Lyft"])
def test_aggressive_cleanup_keeps_whitelisted_mixed_case_names(
    garbage: Any, name: str
) -> None:
    assert garbage.is_garbage_entity(name, "ORGANIZATION", aggressive=True) is False


def test_aggressive_cleanup_still_flags_fragments(garbage: Any) -> None:
    assert garbage.is_garbage_entity("l20", "ORGANIZATION", aggressive=True) is True


class FakeResult:
    def __init__(self, row: dict) -> None:
        self.row = row

    def single(self) -> dict:
        return self.row


class FakeSession:
    def __init__(self, queries: List[str], fail: bool) -> None:
        self.queries = queries
        self.fail = fail

    def __enter__(self) -> "FakeSession":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def run(self, query: str) -> FakeResult:
        if self.fail:
            raise RuntimeError("neo4j unavailable")
        self.queries.append(query)
        return FakeResult({"manual_count": 3, "deleted_count": 3})


class FakeDriver:
    def __init__(self, queries: List[str], fail: bool = False) -> None:
        self.queries = queries
        self.fail = fail

    def session(self) -> FakeSession:
        return FakeSession(self.queries, self.fail)

    def close(self) -> None:
        return None


@pytest.fixture()
def manual_cleanup(monkeypatch: pytest.MonkeyPatch) -> Any:
    module = _load(
        REPO_ROOT / "scripts" / "cleanup_entities.py", "cleanup_entities_under_test"
    )
    monkeypatch.setenv("NEO4J_PASSWORD", "test")
    return module


def _use_driver(
    module: Any, monkeypatch: pytest.MonkeyPatch, driver: FakeDriver
) -> None:
    monkeypatch.setattr(module.GraphDatabase, "driver", lambda *_a, **_k: driver)


def test_manual_cleanup_default_only_counts(
    manual_cleanup: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    queries: List[str] = []
    _use_driver(manual_cleanup, monkeypatch, FakeDriver(queries))
    assert manual_cleanup.cleanup_manual_entities([]) == 0
    assert queries and not any("DELETE" in q for q in queries)


def test_manual_cleanup_deletes_only_with_yes(
    manual_cleanup: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    queries: List[str] = []
    _use_driver(manual_cleanup, monkeypatch, FakeDriver(queries))
    assert manual_cleanup.cleanup_manual_entities(["--yes"]) == 0
    assert any("DETACH DELETE" in q for q in queries)


def test_manual_cleanup_error_exits_non_zero(
    manual_cleanup: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_driver(manual_cleanup, monkeypatch, FakeDriver([], fail=True))
    assert manual_cleanup.cleanup_manual_entities(["--yes"]) != 0


def test_rebuild_probes_the_llm_before_wiping(
    rebuild: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KG_DATABASE_URL", "postgresql://scratch/db")
    calls = Calls(probe_error="401 invalid api key")
    code = rebuild.run(
        ["--yes"],
        load=lambda _url: DOCS,
        wipe=calls.wipe,
        rebuild=calls.rebuild,
        probe=calls.probe,
    )
    assert code != 0
    assert calls.events == ["probe"], "a failing LLM must leave Neo4j untouched"


def test_rebuild_that_extracts_nothing_exits_non_zero(
    rebuild: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KG_DATABASE_URL", "postgresql://scratch/db")
    calls = Calls(entities=0)
    code = rebuild.run(
        ["--yes"],
        load=lambda _url: DOCS,
        wipe=calls.wipe,
        rebuild=calls.rebuild,
        probe=calls.probe,
    )
    assert code != 0


def test_rebuild_with_failed_chunks_exits_non_zero(
    rebuild: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KG_DATABASE_URL", "postgresql://scratch/db")
    calls = Calls(entities=40, failed_chunks=2)
    code = rebuild.run(
        ["--yes"],
        load=lambda _url: DOCS,
        wipe=calls.wipe,
        rebuild=calls.rebuild,
        probe=calls.probe,
    )
    assert code != 0, "a partial extraction after the wipe must not report success"


def test_llm_failures_are_marked_as_errors(
    rebuild: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(**_kwargs: Any) -> dict:
        raise RuntimeError("500 internal error")

    monkeypatch.setattr(rebuild.azure_openai_service, "chat_completion", broken)
    monkeypatch.setattr(rebuild.time, "sleep", lambda _s: None)
    result = rebuild.call_llm([{"role": "user", "content": "x"}], "doc")
    assert result["entities"] == [] and result.get("error") is True


def test_content_filter_skip_is_not_an_error(
    rebuild: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def filtered(**_kwargs: Any) -> dict:
        raise RuntimeError("Error code: 400 - content_filter triggered")

    monkeypatch.setattr(rebuild.azure_openai_service, "chat_completion", filtered)
    result = rebuild.call_llm([{"role": "user", "content": "x"}], "doc")
    assert result.get("error") is not True
    assert result.get("content_filtered") is True


def test_content_filtered_chunks_do_not_count_as_failures(
    rebuild: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    results = iter(
        [
            {
                "entities": [],
                "relationships": [],
                "usage": {},
                "content_filtered": True,
            },
            {
                "entities": [{"name": "A", "type": "concept"}],
                "relationships": [],
                "usage": {},
            },
            {"entities": [], "relationships": [], "usage": {}, "error": True},
        ]
    )
    monkeypatch.setattr(rebuild, "chunk_text", lambda _text: ["c1", "c2", "c3"])
    monkeypatch.setattr(rebuild, "extract_from_chunk", lambda _c, _t: next(results))
    monkeypatch.setattr(
        rebuild,
        "insert_entities_batch",
        lambda ents, _doc: {e["name"]: e["name"] for e in ents},
    )
    monkeypatch.setattr(rebuild, "insert_relationships", lambda *_a: (0, 0))
    monkeypatch.setattr(rebuild.time, "sleep", lambda _s: None)
    docs = [{"id": "d" * 16, "title": "T", "filename": "t.pdf", "content_text": "text"}]
    entities, failed = rebuild.rebuild_documents(docs)
    assert (entities, failed) == (1, 1), "only the real failure counts"


def test_probe_budget_leaves_room_for_reasoning_tokens(
    rebuild: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict = {}

    async def fake(**kwargs: Any) -> dict:
        seen.update(kwargs)
        return {"content": "OK"}

    monkeypatch.setattr(rebuild.azure_openai_service, "chat_completion", fake)
    rebuild.probe_llm()
    assert seen["max_tokens"] >= 256


def test_rebuild_with_yes_wipes_after_validation(
    rebuild: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KG_DATABASE_URL", "postgresql://scratch/db")
    calls = Calls()
    code = rebuild.run(
        ["--yes"],
        load=lambda _url: DOCS,
        wipe=calls.wipe,
        rebuild=calls.rebuild,
        probe=calls.probe,
    )
    assert code == 0
    assert calls.events == ["probe", "wipe", "rebuild:1"]
