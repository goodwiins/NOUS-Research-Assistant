"""Every release-graph writer appends a decision in its transaction (GOO-777).

GOO-721 (#2309) reads the release graph *before* the Collection lock and
trusts it only if the Collection's decision-stream heads are unchanged once
the lock is held (``draft_release_service.resolve_for_staling``). That is
sound only if every write to a graph input also appends a decision to one of
the Collection's streams in the same transaction: ``append_decision`` takes
the stream lock and moves ``next_seq``, so the heads move with the graph.
A writer that skipped it would let a stale snapshot through silently.

This test enforces that rule instead of leaving it in a docstring:

1. The graph-input models are *derived*, not listed: every model named in a
   ``select(...)`` reached from ``draft_release_service._graph`` and from each
   ``graph_part`` it iterates over (following same-module helpers). A new
   graph part or table is covered without editing this file.
2. A *writer* is any function in ``backend/src`` that constructs one of those
   models, or calls ``insert``/``update``/``delete`` on one, or embeds raw
   ``INSERT INTO``/``UPDATE``/``DELETE FROM`` SQL for its table.
3. Each writer must reach ``append_decision`` through its own module's call
   graph, or be a helper whose same-module callers all do.

Not covered: ORM attribute assignment on a loaded row (``row.x = ...``) is
not visible to an AST scan. Graph rows are append-only by design (new
versions supersede old ones), and ``invalidate_dependents`` is the one
sanctioned in-place update (``update(DraftRelease)``), which is covered.

Mutation check (GOO-777): adding ``def _rogue(): return DraftRelease()`` to
any ``src`` module makes ``test_every_graph_writer_appends_a_decision`` fail
and name it.
"""

from __future__ import annotations

import ast
import re
from functools import lru_cache
from pathlib import Path
from typing import Iterator

import pytest

pytestmark = pytest.mark.unit

SRC = Path(__file__).resolve().parents[3] / "src"
GRAPH_MODULE = SRC / "services/research/draft_release_service.py"
GRAPH_FUNCTION = "_graph"
APPEND = "append_decision"
WRITE_CALLS = frozenset({"insert", "update", "delete", "pg_insert"})

# Read by a graph reader but not a graph *input* in the heads sense. Each
# entry must still be read (the test fails if it goes stale).
NOT_GRAPH_INPUTS: dict[str, str] = {
    # experiment_service.graph_part reads checksums to build the ``changed``
    # set (which document inputs moved). ``changed`` is computed by the
    # staling writer under the lock, never taken from the pre-lock snapshot.
    "Document": "feeds only the changed set, read under the lock",
    # _project_extraction joins it only to scope form versions to the
    # Collection. A new matrix has no accepted values, so it adds no edge;
    # its form versions and accepted values are graph inputs and checked.
    "ExtractionMatrix": "join filter only; adds no edge by itself",
    # Read to decide whether a protocol is still current (the changed set).
    # Edges name a protocol by the id stored on their own row (evidence
    # table, synthesis result), and approving a version appends a decision.
    "ResearchProtocolVersion": "feeds only the changed set (is it current)",
}

# A floor for the derivation: if the AST walk silently finds less than this,
# the derivation broke, not the rule.
KNOWN_GRAPH_INPUTS = frozenset(
    {
        "DraftRelease",
        "ExtractionAcceptedValue",
        "ResearchClaimAssessment",
        "ResearchClaimEvidenceLink",
        "AppraisalAssessment",
        "SynthesisResult",
        "ResearchFigure",
        "ResearchRunManifest",
        "ManuscriptRelease",
        # Built from a parameter (evidence_service._state's ``ordered``).
        "EvidenceTableVersion",
        "EvidenceContradiction",
        "OutcomeCertaintyAssessment",
        # Named through a module-level alias (``V = ResearchReviewVersion``).
        "ResearchReviewVersion",
    }
)

Function = ast.FunctionDef | ast.AsyncFunctionDef


@lru_cache(maxsize=None)
def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text())


def _functions(tree: ast.AST) -> dict[str, Function]:
    return {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _called_names(node: ast.AST) -> set[str]:
    names = set()
    for call in ast.walk(node):
        if isinstance(call, ast.Call):
            if isinstance(call.func, ast.Name):
                names.add(call.func.id)
            elif isinstance(call.func, ast.Attribute):
                names.add(call.func.attr)
    return names


@lru_cache(maxsize=1)
def _mapped_models() -> frozenset[str]:
    import src.models  # noqa: F401  register every mapped class
    from src.models.base import Base

    return frozenset(mapper.class_.__name__ for mapper in Base.registry.mappers)


def _import_aliases(tree: ast.AST) -> dict[str, str]:
    """``from x import Model as M`` and a module-level ``M = Model`` ->
    ``{"M": "Model"}`` (and identity)."""
    aliases = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                aliases[alias.asname or alias.name] = alias.name
    for node in getattr(tree, "body", []):
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Name)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            target = node.value.id
            aliases[node.targets[0].id] = aliases.get(target, target)
    return aliases


def _model_name(node: ast.expr, aliases: dict[str, str]) -> str | None:
    """``Model``, ``Model.column`` or an alias of either -> ``"Model"``."""
    if isinstance(node, ast.Attribute):
        node = node.value
    if isinstance(node, ast.Name):
        name = aliases.get(node.id, node.id)
        if name in _mapped_models():
            return name
    return None


def _reader_models(path: Path, entry: str) -> set[str]:
    """Every mapped model named in ``entry`` and the same-module helpers it
    calls. Naming, not just ``select(Model)``: a helper may build the query
    from a parameter (``ordered(EvidenceTableVersion)``)."""
    tree = _parse(path)
    aliases = _import_aliases(tree)
    functions = _functions(tree)
    seen, todo, models = set(), [entry], set()
    while todo:
        name = todo.pop()
        if name in seen or name not in functions:
            continue
        seen.add(name)
        for node in ast.walk(functions[name]):
            if isinstance(node, ast.Name) and (model := _model_name(node, aliases)):
                models.add(model)
        todo += list(_called_names(functions[name]) & set(functions))
    return models


def _graph_part_modules() -> list[Path]:
    """The modules whose ``graph_part`` ``_graph`` iterates over, resolved
    through ``_graph``'s own local imports."""
    graph = _functions(ast.parse(GRAPH_MODULE.read_text()))[GRAPH_FUNCTION]
    imported: dict[str, str] = {}
    for node in ast.walk(graph):
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                imported[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    parts = []
    for node in ast.walk(graph):
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "graph_part"
            and isinstance(node.value, ast.Name)
        ):
            dotted = imported[node.value.id]
            parts.append(SRC.parent / (dotted.replace(".", "/") + ".py"))
    return parts


@lru_cache(maxsize=1)
def _graph_models() -> frozenset[str]:
    models = _reader_models(GRAPH_MODULE, GRAPH_FUNCTION)
    for path in _graph_part_modules():
        models |= _reader_models(path, "graph_part")
    return frozenset(models)


def _graph_inputs() -> frozenset[str]:
    return _graph_models() - set(NOT_GRAPH_INPUTS)


@lru_cache(maxsize=1)
def _table_names() -> dict[str, str]:
    from src.models.base import Base

    tables = {}
    for mapper in Base.registry.mappers:
        cls = mapper.class_
        if cls.__name__ in _graph_inputs():
            tables[cls.__name__] = str(getattr(cls, "__tablename__"))
    return tables


@lru_cache(maxsize=1)
def _raw_sql_pattern() -> re.Pattern[str]:
    tables = "|".join(sorted(map(re.escape, _table_names().values())))
    return re.compile(
        rf"\b(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+({tables})\b", re.IGNORECASE
    )


def _raw_sql_writes(fn: Function) -> set[str]:
    found = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            for match in _raw_sql_pattern().finditer(node.value):
                found.add(f"sql({match.group(1).lower()})")
    return found


def _writes(fn: Function, models: frozenset[str], aliases: dict[str, str]) -> set[str]:
    found = set()
    for call in ast.walk(fn):
        if not isinstance(call, ast.Call):
            continue
        func = call.func
        if isinstance(func, ast.Name) and aliases.get(func.id, func.id) in models:
            found.add(aliases.get(func.id, func.id))
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if (
            name in WRITE_CALLS
            and call.args
            and isinstance(call.args[0], ast.Name)
            and (model := _model_name(call.args[0], aliases)) in models
        ):
            found.add(f"{name}({model})")
    return found | _raw_sql_writes(fn)


def _violations_in(path: Path, models: frozenset[str]) -> Iterator[str]:
    tree = _parse(path)
    aliases = _import_aliases(tree)
    functions = _functions(tree)
    local = set(functions)
    calls = {name: _called_names(fn) & local for name, fn in functions.items()}
    reaches = {name for name, fn in functions.items() if APPEND in _called_names(fn)}
    grew = True
    while grew:
        grew = False
        for name, callees in calls.items():
            if name not in reaches and callees & reaches:
                reaches.add(name)
                grew = True
    callers = {name: {c for c, cs in calls.items() if name in cs} for name in local}

    def appends(name: str, seen: frozenset[str]) -> bool:
        if name in reaches:
            return True
        up = callers[name] - seen - {name}
        return bool(up) and all(appends(c, seen | {name}) for c in up)

    for name, fn in functions.items():
        written = _writes(fn, models, aliases)
        if written and not appends(name, frozenset()):
            rel = path.relative_to(SRC.parent)
            yield f"{rel}:{fn.lineno} {name} writes {sorted(written)}"


def test_graph_inputs_are_derived_from_the_graph_readers() -> None:
    assert _graph_part_modules(), "found no graph_part modules in _graph"
    missing = KNOWN_GRAPH_INPUTS - _graph_inputs()
    assert not missing, f"derivation lost graph inputs: {sorted(missing)}"


def test_not_graph_inputs_entries_are_still_read() -> None:
    stale = set(NOT_GRAPH_INPUTS) - _graph_models()
    assert not stale, f"remove from NOT_GRAPH_INPUTS: {sorted(stale)}"


def test_every_graph_writer_appends_a_decision() -> None:
    models = _graph_inputs()
    violations = [
        violation
        for path in sorted(SRC.rglob("*.py"))
        for violation in _violations_in(path, models)
    ]
    assert not violations, (
        "These functions write a release-graph input without appending a "
        "decision (append_decision) in the same transaction, so the "
        "Collection's stream heads would not move and resolve_for_staling "
        "could trust a stale pre-lock snapshot (GOO-721/GOO-777):\n"
        + "\n".join(violations)
    )


def test_the_scan_finds_the_known_writers() -> None:
    """Guards the scan itself: it must see the writers we know exist."""
    models = _graph_inputs()
    seen = set()
    for path in SRC.rglob("*.py"):
        tree = _parse(path)
        aliases = _import_aliases(tree)
        for name, fn in _functions(tree).items():
            if _writes(fn, models, aliases):
                seen.add(f"{path.name}:{name}")
    expected = {
        "draft_release_service.py:promote",
        "draft_release_service.py:invalidate_dependents",
        "claims_service.py:link",
        "claims_service.py:assess",
        "extraction_forms_service.py:accept_value",
        "synthesis_service.py:execute",
        "experiment_service.py:register_figure",
    }
    assert expected <= seen, sorted(expected - seen)
