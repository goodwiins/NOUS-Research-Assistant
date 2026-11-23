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
4. Each writer must also hold the Collection lock, or the heads check proves
   nothing: it reaches a lock through its own calls, or every caller in
   ``src`` does (calls resolved across modules through imports). The lock
   is ``lock_active_project``, ``resolve_for_staling``, or ``resolve_project``
   with a literal action in ``project_access._MUTATING_ACTIONS``, called
   directly, through a local alias, or through a wrapper that forwards its
   ``action`` parameter. The lock functions are terminal: ``resolve_project``
   calls ``lock_active_project`` itself, so passing through it would count a
   ``VIEW`` caller as locked.

Not covered: ORM attribute assignment on a loaded row (``row.x = ...``) is
not visible to an AST scan. Graph rows are append-only by design (new
versions supersede old ones), and ``invalidate_dependents`` is the one
sanctioned in-place update (``update(DraftRelease)``), which is covered.

Mutation check (GOO-777): adding ``def _rogue(): return DraftRelease()`` to
any ``src`` module makes ``test_every_graph_writer_appends_a_decision`` fail
and name it. So does each lock mutant, one at a time:
- a planted writer that calls ``append_decision`` but takes no lock
- the figure route resolving with ``resolve_project(..., VIEW)``
- the claim-link route's aliased ``resolve(..., VIEW)``
- the observation route calling ``_matrix_context(..., VIEW)``
- ``_matrix_context`` passing ``VIEW`` instead of forwarding ``action``
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
# The Collection lock: ``resolve_project`` with a mutating action takes it
# (Workspace SHARE -> Collection UPDATE); these two always do.
LOCKS = frozenset({"lock_active_project", "resolve_for_staling"})
RESOLVE = "resolve_project"

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


Key = tuple[Path, str]


def _module_path(dotted: str) -> Path | None:
    base = SRC.parent / dotted.replace(".", "/")
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


@lru_cache(maxsize=None)
def _imports(path: Path) -> dict[str, tuple[Path, str | None]]:
    """Local name -> (module file, None) for an imported module, or
    (module file, function name) for an imported function."""
    package = path.parent.relative_to(SRC.parent).parts
    out: dict[str, tuple[Path, str | None]] = {}
    for node in ast.walk(_parse(path)):
        if isinstance(node, ast.ImportFrom):
            parts = package[: len(package) - node.level + 1] if node.level else ()
            module = ".".join((*parts, *(node.module or "").split("."))).strip(".")
            for alias in node.names:
                local = alias.asname or alias.name
                if sub := _module_path(f"{module}.{alias.name}"):
                    out[local] = (sub, None)
                elif parent := _module_path(module):
                    out[local] = (parent, alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname and (mod := _module_path(alias.name)):
                    out[alias.asname] = (mod, None)
    return out


def _resolve(call: ast.Call, path: Path, local: dict[str, Function]) -> Key | None:
    """The function a call lands on, across modules, when the AST names it."""
    func, imports = call.func, _imports(path)
    if isinstance(func, ast.Name):
        if func.id in local:
            return (path, func.id)
        module, name = imports.get(func.id, (None, None))
        if module and name:
            return (module, name)
    elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        if func.value.id in {"self", "cls"}:
            return (path, func.attr)
        module, name = imports.get(func.value.id, (None, None))
        if module and name is None:
            return (module, func.attr)
    return None


def _call_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    return getattr(call.func, "attr", None)


@lru_cache(maxsize=1)
def _mutating_actions() -> frozenset[str]:
    from src.services.research_engine.project_access import _MUTATING_ACTIONS

    return frozenset(action.name for action in _MUTATING_ACTIONS)


def _mutating(call: ast.Call) -> bool:
    """``resolve_project`` locks the Collection only for a mutating action,
    so the call must pass one as a literal ``ResearchAction.X``. A variable
    action proves nothing here; a wrapper that forwards its own ``action``
    parameter is judged at its call sites instead (``_action_forwarders``)."""
    return any(
        isinstance(arg, ast.Attribute)
        and isinstance(arg.value, ast.Name)
        and arg.value.id == "ResearchAction"
        and arg.attr in _mutating_actions()
        for arg in [*call.args, *(k.value for k in call.keywords)]
    )


@lru_cache(maxsize=1)
def _action_forwarders() -> frozenset[str]:
    """Functions with an ``action`` parameter that pass it straight to
    ``resolve_project`` (e.g. ``extraction_matrix._matrix_context``): a call
    to one with a literal mutating action takes the lock."""
    found = set()
    for path in SRC.rglob("*.py"):
        for name, fn in _functions(_parse(path)).items():
            if name == RESOLVE or "action" not in {a.arg for a in fn.args.args}:
                continue
            for call in ast.walk(fn):
                if (
                    isinstance(call, ast.Call)
                    and _call_name(call) == RESOLVE
                    and any(
                        isinstance(arg, ast.Name) and arg.id == "action"
                        for arg in [*call.args, *(k.value for k in call.keywords)]
                    )
                ):
                    found.add(name)
    return frozenset(found)


def _takes_lock(fn: Function) -> bool:
    """Calls ``lock_active_project`` or ``resolve_for_staling``, or calls
    ``resolve_project`` with a literal mutating ``ResearchAction``: directly,
    through a local alias (``resolve = resolve_for_staling if ... else
    resolve_project``) or through an action-forwarding wrapper."""
    resolvers = {RESOLVE} | _action_forwarders()
    for node in ast.walk(fn):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            named = {
                n.id if isinstance(n, ast.Name) else n.attr
                for n in ast.walk(node.value)
                if isinstance(n, (ast.Name, ast.Attribute))
            }
            if named & (LOCKS | {RESOLVE}):
                resolvers.add(node.targets[0].id)
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            name = _call_name(node)
            if name in LOCKS or (name in resolvers and _mutating(node)):
                return True
    return False


@lru_cache(maxsize=1)
def _lock_graph() -> tuple[dict[Key, set[Key]], frozenset[Key]]:
    """(callers of each function, functions that reach a Collection lock
    through their own calls), over every function in ``src``."""
    # The primitives are terminal: ``resolve_project`` itself calls
    # ``lock_active_project``, so propagating through it would count a
    # ``resolve_project(..., VIEW)`` caller as locked. Only ``_takes_lock``
    # judges a call to one.
    primitives = LOCKS | {RESOLVE}
    edges: dict[Key, set[Key]] = {}
    reaches: set[Key] = set()
    for path in SRC.rglob("*.py"):
        local = _functions(_parse(path))
        for name, fn in local.items():
            edges[(path, name)] = {
                key
                for call in ast.walk(fn)
                if isinstance(call, ast.Call)
                and (key := _resolve(call, path, local))
                and key[1] not in primitives
            }
            if name not in primitives and _takes_lock(fn):
                reaches.add((path, name))
    grew = True
    while grew:
        grew = False
        for key, callees in edges.items():
            if key not in reaches and callees & reaches:
                reaches.add(key)
                grew = True
    callers: dict[Key, set[Key]] = {key: set() for key in edges}
    for key, callees in edges.items():
        for callee in callees:
            callers.setdefault(callee, set()).add(key)
    return callers, frozenset(reaches)


def _holds_lock(key: Key, seen: frozenset[Key] = frozenset()) -> bool:
    """Reaches a Collection lock itself, or every caller (in any module)
    does. A function nothing calls must take the lock itself."""
    callers, reaches = _lock_graph()
    if key in reaches:
        return True
    up = callers.get(key, set()) - seen - {key}
    return bool(up) and all(_holds_lock(c, seen | {key}) for c in up)


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
        if not written:
            continue
        rel = path.relative_to(SRC.parent)
        if not appends(name, frozenset()):
            yield f"{rel}:{fn.lineno} {name} writes {sorted(written)} without append_decision"
        if not _holds_lock((path, name)):
            yield f"{rel}:{fn.lineno} {name} writes {sorted(written)} without a Collection lock"


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
        "decision (append_decision) in the same transaction, or without "
        "holding the Collection lock, so resolve_for_staling could trust a "
        "stale pre-lock snapshot (GOO-721/GOO-777):\n" + "\n".join(violations)
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
