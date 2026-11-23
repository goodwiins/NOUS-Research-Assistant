"""GOO-693: blocking calls that were moved off the event loop stay off it.

GOO-641/642/643 wrapped these sync calls (S3, Celery broker, sync-DB Session,
PDF parsing) in ``asyncio.to_thread`` / ``run_in_threadpool``. Nothing failed
if a later edit called one directly again inside the ``async def``. This test
reads the source and, for each site, checks:

* the call is passed to an offload helper inside that function, and
* the function does not also call it directly.

Sites that use the request's sync ``Session`` (or an upload whose object must
be deleted after a cancel) must use ``to_thread_finish_on_cancel``: a plain
``to_thread`` returns on cancellation while the thread is still inside the
Session that ``get_db_sync`` is about to close. They must also pass the
Session as ``guard_session=`` (GOO-698), so a thread abandoned after the
bounded wait starts no COMMIT on it and is never closed under.

GOO-766 adds a scan of every offload call under src/ (end of this file): any
``to_thread``, ``run_in_executor`` or ``run_in_threadpool`` that takes a sync
Session fails unless it is listed in SESSION_OFFLOAD_EXEMPTIONS with a reason.

GOO-783 resolves names before matching, so these forms are caught too (each
has a planted test, ``_EVASIONS``): aliased imports of the offload helpers,
``anyio.to_thread.run_sync`` however imported, ``<executor>.submit``, a
``partial(...)`` (or any value) bound to a name and offloaded through it, and
a Session parameter known only by ``Depends(get_db_sync)``.

GOO-789 follows calls into project functions that forward a parameter to an
offload helper (``async def _offload(fn, *a): return await
asyncio.to_thread(fn, *a)``), wrappers of wrappers included, and judges the
call site ``_offload(db.commit)`` as the offload it wraps. The known limits
of that resolution are listed above ``_Forwarder`` and pinned by a test.
"""

import ast
from pathlib import Path
from typing import Dict, FrozenSet, List, NamedTuple, Optional, Set, Tuple

import pytest

pytestmark = pytest.mark.unit

_SRC = Path(__file__).resolve().parents[3] / "src"

_ANY_OFFLOAD = frozenset(
    {"to_thread", "run_in_threadpool", "to_thread_finish_on_cancel"}
)
_FINISH = frozenset({"to_thread_finish_on_cancel"})


class Site(NamedTuple):
    path: str
    function: str
    # A callable passed by reference ("calculate_file_hash"), or
    # "lambda:<attr>" for a lambda whose body calls ``<x>.<attr>(...)``.
    target: str
    offloads: frozenset = _ANY_OFFLOAD
    # The call must pass the request Session as ``guard_session=`` so an
    # abandoned thread cannot commit on it or have it closed under it
    # (GOO-698).
    guards_session: bool = False


SITES: List[Site] = [
    # S3 / storage (GOO-643)
    Site("services/documents/file_service.py", "upload_file", "calculate_file_hash"),
    Site("services/documents/file_service.py", "upload_file", "_delete_stored_object"),
    Site("services/documents/file_service.py", "delete_file", "delete_physical_file"),
    Site(
        "services/documents/file_service.py",
        "_upload_off_loop",
        "_upload_spool",
        _FINISH,
    ),
    # Celery broker publishes (GOO-643)
    Site("api/infrastructure/evaluation.py", "create_evaluation_job", "delay"),
    Site("api/documents/processing.py", "cancel_processing_job", "revoke"),
    Site("api/research/projects.py", "add_document_to_project", "apply_async"),
    # Sync search / DB in async handlers (GOO-643)
    Site("api/search/search.py", "search_health_check", "search"),
    Site("api/search/search.py", "search_health_check", "lambda:execute"),
    # PDF parsing (GOO-642)
    Site(
        "api/arxiv/arxiv_local.py",
        "_extract_features_from_local_pdfs",
        "_read_pdf_text_and_metadata",
    ),
    Site(
        "api/arxiv/arxiv_local.py",
        "_post_process_extraction",
        "_get_shared_embedding_service",
    ),
    # Request-scoped sync Session (GOO-693: must outlive cancellation)
    Site(
        "api/evidence/router.py",
        "get_evidence_meter",
        "_load_sources_and_release",
        _FINISH,
        True,
    ),
    Site(
        "api/evidence/router.py",
        "get_evidence_meter",
        "_save_and_commit",
        _FINISH,
        True,
    ),
    Site(
        "api/evidence/router.py",
        "classify_sources_for_claim",
        "_load_sources_and_release",
        _FINISH,
        True,
    ),
    Site(
        "api/evidence/router.py",
        "classify_sources_for_claim",
        "_save_and_commit",
        _FINISH,
        True,
    ),
    Site(
        "api/evidence/router.py",
        "classify_sources_for_claim",
        "rollback",
        _FINISH,
        True,
    ),
    Site(
        "api/infrastructure/evaluation.py",
        "create_evaluation_job",
        "_fail_job_quietly",
        _FINISH,
        True,
    ),
    Site(
        "services/evaluation/rag_evaluation_service.py",
        "create_evaluation_job",
        "_create_evaluation_job_sync",
        _FINISH,
        True,
    ),
    Site(
        "api/quality/user_behavior.py",
        "get_user_behavior_analytics",
        "lambda:query",
        _FINISH,
        True,
    ),
    Site(
        "api/quality/user_behavior.py",
        "get_organization_user_behavior",
        "lambda:execute",
        _FINISH,
        True,
    ),
    Site(
        "api/quality/user_behavior.py",
        "generate_behavior_report",
        "lambda:query",
        _FINISH,
        True,
    ),
    Site(
        "api/quality/user_behavior.py",
        "export_behavior_data",
        "_query_content_usage",
        _FINISH,
        True,
    ),
]

_TREES: Dict[str, ast.Module] = {}


def _tree(path: str) -> ast.Module:
    if path not in _TREES:
        _TREES[path] = ast.parse((_SRC / path).read_text())
    return _TREES[path]


def _tail(node: ast.AST) -> Optional[str]:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _function(tree: ast.Module, name: str) -> ast.AsyncFunctionDef:
    found = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == name
    ]
    assert len(found) == 1, f"expected one async def {name}, found {len(found)}"
    return found[0]


def _calls_attr(node: ast.AST, attr: str) -> bool:
    return any(
        isinstance(n, ast.Call) and _tail(n.func) == attr for n in ast.walk(node)
    )


def _offload_calls(fn: ast.AST) -> List[Tuple[str, ast.expr, ast.Call]]:
    out = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and node.args:
            name = _tail(node.func)
            if name in _ANY_OFFLOAD:
                out.append((name, node.args[0], node))
    return out


def _passes_session_as_guard(call: ast.Call, fn: ast.AST) -> bool:
    """``guard_session=`` names a sync Session of ``fn`` (any name, GOO-766)."""
    return any(
        kw.arg == "guard_session"
        and isinstance(kw.value, ast.Name)
        and kw.value.id in _sync_session_names(fn)
        for kw in call.keywords
    )


def _matches(arg: ast.expr, target: str) -> bool:
    if target.startswith("lambda:"):
        return isinstance(arg, ast.Lambda) and _calls_attr(arg.body, target[7:])
    return _tail(arg) == target


@pytest.mark.parametrize(
    "site", SITES, ids=lambda s: f"{s.path}:{s.function}:{s.target}"
)
def test_blocking_call_stays_off_the_loop(site: Site) -> None:
    fn = _function(_tree(site.path), site.function)
    offloads = _offload_calls(fn)
    used = [name for name, arg, _ in offloads if _matches(arg, site.target)]

    assert used, f"{site.target} is no longer offloaded in {site.function}"
    wrong = [name for name in used if name not in site.offloads]
    assert (
        not wrong
    ), f"{site.target} must use one of {sorted(site.offloads)}, not {wrong}"
    if site.guards_session:
        unguarded = [
            call.lineno
            for _, arg, call in offloads
            if _matches(arg, site.target) and not _passes_session_as_guard(call, fn)
        ]
        assert not unguarded, (
            f"{site.target} uses the request Session but does not pass it "
            f"as guard_session= (lines {unguarded})"
        )

    lambdas = {
        id(n)
        for _, arg, _ in offloads
        if isinstance(arg, ast.Lambda)
        for n in ast.walk(arg)
    }
    if site.target.startswith("lambda:"):
        # The lambda wraps a sync-Session call (``db.<attr>(...)``); the same
        # call made outside an offloaded lambda would run on the loop.
        attr = site.target[len("lambda:") :]
        direct = [
            n.lineno
            for n in ast.walk(fn)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == attr
            and isinstance(n.func.value, ast.Name)
            and n.func.value.id == "db"
            and id(n) not in lambdas
        ]
    else:
        direct = [
            n.lineno
            for n in ast.walk(fn)
            if isinstance(n, ast.Call)
            and _tail(n.func) == site.target
            and id(n) not in lambdas
        ]
    assert not direct, f"{site.target} called directly on the loop at lines {direct}"


# ---- GOO-766: every offload that takes a sync Session, not just the list ----
#
# The SITES list above pins known call sites. A new raw ``to_thread`` or
# ``run_in_executor`` that handed the request Session to a thread passed it.
# This scan reads every offload call under src/ and fails any that takes a
# sync Session (directly, in a lambda, through partial(...), in a nested def
# it closes over, or in a ``self`` method that uses ``self.db``) unless it is
# ``to_thread_finish_on_cancel`` with that Session as ``guard_session=``.

_RAW_OFFLOADS = frozenset(
    {
        "to_thread",
        "run_in_executor",
        "run_in_threadpool",
        # GOO-783: ``anyio.to_thread.run_sync`` (only under ``to_thread``:
        # ``anyio.from_thread.run_sync`` runs on the loop) and
        # ``<executor>.submit``.
        "run_sync",
        "submit",
    }
)
# Sync-Session dependencies: a parameter defaulting to Depends(<one>) is a
# Session whatever it is called or annotated (GOO-783).
_SYNC_SESSION_DEPENDENCIES = frozenset({"get_db_sync"})
_GUARDED_OFFLOAD = "to_thread_finish_on_cancel"
# Conventional names of a sync Session in this codebase.
_SESSION_NAMES = frozenset({"db", "db_session", "sync_db", "sync_session"})
# ``self.<attr>`` holding a Session.
_SELF_SESSION_ATTRS = frozenset({"db", "_db", "session", "_session", "db_session"})
# Keyword arguments of to_thread_finish_on_cancel itself, not of the work.
_HELPER_KWARGS = frozenset({"guard_session", "on_cancel", "max_wait"})

# "<path>:<function>:<offload>:<callable>" -> why it may stay unguarded.
SESSION_OFFLOAD_EXEMPTIONS: Dict[str, str] = {
    "core/thread_offload.py:_start:to_thread:_run": (
        "the guarded helper's own thread: _run is the work guard_session protects"
    ),
    (
        "services/agent/tools_impl.py:_tool_extract_entities:to_thread:"
        "file_service.extract_text_content"
    ): (
        "seen through file_service = FileService(db) (GOO-783 follows names): "
        "extract_text_content reads only the document's file and never uses "
        "FileService.db, and this db is the agent tool's AsyncSession"
    ),
}


def _annotation(arg: ast.arg) -> str:
    return ast.unparse(arg.annotation) if arg.annotation is not None else ""


def _aliases(tree: ast.AST) -> Dict[str, str]:
    """Local name -> dotted origin, from this module's imports (GOO-783):
    ``from asyncio import to_thread as off`` gives ``off -> asyncio.to_thread``,
    ``import asyncio as aio`` gives ``aio -> asyncio``."""
    out: Dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                out[alias.asname or alias.name] = f"{node.module}.{alias.name}"
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    out[alias.asname] = alias.name
                else:
                    head = alias.name.split(".")[0]
                    out[head] = head
    return out


def _dotted(node: ast.AST, aliases: Dict[str, str]) -> Optional[str]:
    """``aio.to_thread`` -> ``asyncio.to_thread``; None for a non-name."""
    parts: List[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(aliases.get(node.id, node.id))
    return ".".join(reversed(parts))


def _offload_kind(call: ast.Call, aliases: Dict[str, str]) -> Optional[str]:
    """The offload helper ``call`` resolves to, or None."""
    dotted = _dotted(call.func, aliases) or (_tail(call.func) or "")
    *parents, last = dotted.split(".")
    if last == "run_sync":
        return last if "to_thread" in parents else None
    if last in _RAW_OFFLOADS or last == _GUARDED_OFFLOAD:
        return last
    return None


def _depends_on_sync_session(
    default: Optional[ast.expr], aliases: Dict[str, str]
) -> bool:
    """``Depends(get_db_sync)`` (or an alias of it) as a parameter default."""
    if not (
        isinstance(default, ast.Call)
        and _tail(default.func) == "Depends"
        and default.args
    ):
        return False
    dependency = _dotted(default.args[0], aliases) or ""
    return dependency.split(".")[-1] in _SYNC_SESSION_DEPENDENCIES


def _sync_session_names(
    fn: ast.AST, aliases: Optional[Dict[str, str]] = None
) -> Set[str]:
    """Names that hold a sync Session inside ``fn``."""
    names = set(_SESSION_NAMES)
    if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
        positional = fn.args.posonlyargs + fn.args.args
        defaults: List[Optional[ast.expr]] = [None] * (
            len(positional) - len(fn.args.defaults)
        ) + list(fn.args.defaults)
        pairs = list(zip(positional, defaults)) + list(
            zip(fn.args.kwonlyargs, fn.args.kw_defaults)
        )
        for arg, default in pairs:
            ann = _annotation(arg)
            if "AsyncSession" in ann:
                names.discard(arg.arg)
            elif "Session" in ann or _depends_on_sync_session(default, aliases or {}):
                names.add(arg.arg)
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            callee = _tail(node.value.func) or ""
            if "Session" in callee and "Async" not in callee:
                names |= {t.id for t in node.targets if isinstance(t, ast.Name)}
    return names


def _refs(node: ast.AST, names: Set[str]) -> Set[str]:
    """Session references in ``node``, skipping what is awaited on the loop."""
    found: Set[str] = set()
    stack = [node]
    while stack:
        cur = stack.pop()
        if isinstance(cur, ast.Await):
            continue  # evaluated on the loop before the offload
        if isinstance(cur, ast.Name) and cur.id in names:
            found.add(cur.id)
        if (
            isinstance(cur, ast.Attribute)
            and isinstance(cur.value, ast.Name)
            and cur.value.id == "self"
            and cur.attr in _SELF_SESSION_ATTRS
        ):
            found.add(f"self.{cur.attr}")
        stack.extend(ast.iter_child_nodes(cur))
    return found


_Scope = Tuple[ast.AST, Optional[ast.ClassDef]]


def _scopes(tree: ast.Module) -> Dict[int, _Scope]:
    """For every node: its innermost enclosing function and class."""
    out: Dict[int, _Scope] = {}

    def visit(node: ast.AST, fn: ast.AST, cls: Optional[ast.ClassDef]) -> None:
        for child in ast.iter_child_nodes(node):
            out[id(child)] = (fn, cls)
            if isinstance(child, ast.ClassDef):
                visit(child, fn, child)
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                visit(child, child, cls)
            else:
                visit(child, fn, cls)

    visit(tree, tree, None)
    return out


def _offload_args(call: ast.Call) -> List[ast.expr]:
    args = list(call.args)
    return args[1:] if _tail(call.func) == "run_in_executor" else args


def _bound_values(fn: ast.AST) -> Dict[str, List[ast.expr]]:
    """``name = <value>`` assignments inside ``fn``, by name."""
    out: Dict[str, List[ast.expr]] = {}
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    out.setdefault(target.id, []).append(node.value)
    return out


def _refs_through_names(
    nodes: List[ast.expr], names: Set[str], bound: Dict[str, List[ast.expr]]
) -> Set[str]:
    """Session references in ``nodes``, following a name to the value bound
    to it (``job = partial(work, db)`` then ``to_thread(job)``, GOO-783).
    Awaited values were computed on the loop and are skipped, as in _refs."""
    found: Set[str] = set()
    seen: Set[str] = set()
    todo = list(nodes)
    while todo:
        node = todo.pop()
        found |= _refs(node, names)
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name) and sub.id in bound and sub.id not in seen:
                seen.add(sub.id)
                todo += [v for v in bound[sub.id] if not isinstance(v, ast.Await)]
    return found


def _work_args(call: ast.Call) -> List[ast.expr]:
    """What an offload call hands its thread: the callable and its arguments,
    not the guarded helper's own keywords."""
    return _offload_args(call) + [
        kw.value for kw in call.keywords if kw.arg not in _HELPER_KWARGS
    ]


def _touched_sessions(
    call: ast.Call,
    fn: ast.AST,
    cls: Optional[ast.ClassDef],
    aliases: Optional[Dict[str, str]] = None,
) -> Set[str]:
    return _touched_by(_work_args(call), fn, cls, aliases)


def _touched_by(
    work: List[ast.expr],
    fn: ast.AST,
    cls: Optional[ast.ClassDef],
    aliases: Optional[Dict[str, str]] = None,
) -> Set[str]:
    """Sessions that ``work`` (the callable first, then its arguments) takes
    into the thread."""
    names = _sync_session_names(fn, aliases)
    touched = _refs_through_names(work, names, _bound_values(fn))
    target = work[0] if work else None
    if isinstance(target, ast.Name):  # a nested def closing over the Session
        for node in ast.walk(fn):
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node is not fn
                and node.name == target.id
            ):
                touched |= _refs(node, names)
    if (
        isinstance(target, ast.Attribute)
        and isinstance(target.value, ast.Name)
        and target.value.id == "self"
        and cls is not None
    ):
        for node in cls.body:
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == target.attr
            ):
                touched |= _refs(node, set())  # its self.<session attr> uses
    return touched


# ---- GOO-789: project wrappers that forward their callable to a thread ----
#
# ``async def _offload(fn, *a): return await asyncio.to_thread(fn, *a)`` hid
# the offload: the call site ``await _offload(db.commit)`` names no offload
# helper, and inside the wrapper ``fn`` is no Session. As in the GOO-777
# release-graph test (``_action_forwarders``), a *forwarder* is a function
# that passes one of its own parameters straight to the primitive, and its
# call sites are judged instead: the arguments bound to the forwarded
# parameters are the work handed to the thread. Forwarders compose (a wrapper
# of a wrapper) through a fixpoint over the module's functions, as GOO-777
# closes its call graph. A call resolves to a forwarder through a bare name in
# the same module, ``self.<method>``/``cls.<method>`` of the enclosing class,
# or a ``src.*`` import (``from src.x import f`` or ``import src.x as m``). A
# module-level alias of a forwarder (``run_in_subprocess = _run_in_subprocess``
# in arxiv_fulltext) is a forwarder under its new name too.
#
# Known limits (``test_known_limits_of_forwarder_resolution`` pins them):
# - a forwarder reached through another object (``svc._offload(db.commit)``);
# - one reached through a variable bound inside a function (``run = _offload``
#   in the caller's body) or through ``getattr``;
# - a forwarder defined inside a function (only module-level functions and
#   class methods are scanned), so a nested ``async def _offload`` is unseen;
# - one that stores the callable (``self._job = fn``) and offloads it later.
# None of these exists in src today; a new one needs its call sites listed in
# SITES above or the resolution extended.

_SELF_NAMES = frozenset({"self", "cls"})


class _Forwarder(NamedTuple):
    positional: Tuple[str, ...]  # positional parameter names, in order
    keyword_only: Tuple[str, ...]
    vararg: Optional[str]
    kwarg: Optional[str]
    forwarded: FrozenSet[str]  # parameters that reach a thread
    # ``to_thread_finish_on_cancel(..., guard_session=<param>)``: the caller
    # must pass the Session it hands over as this parameter. None: the
    # forwarding paths do not all guard through one parameter (a raw offload,
    # a guarded one without guard_session=, or two different guards).
    guard: Optional[str]
    bound_method: bool  # called as self.<m>: the first parameter is implicit


_Resolved = Tuple[str, _Forwarder]


def _bind(call: ast.Call, fwd: _Forwarder) -> Tuple[List[ast.expr], Optional[ast.expr]]:
    """(arguments bound to forwarded parameters, argument bound to the
    guard parameter) for a call to ``fwd``. A ``*args`` or ``**kwargs`` at
    the call counts as forwarded when it can reach a forwarded parameter."""
    positional = fwd.positional[1:] if fwd.bound_method else fwd.positional
    work: List[ast.expr] = []
    guard: Optional[ast.expr] = None
    for i, arg in enumerate(call.args):
        if isinstance(arg, ast.Starred):
            rest = set(positional[i:]) | ({fwd.vararg} if fwd.vararg else set())
            if rest & fwd.forwarded:
                work.append(arg.value)
            continue
        param = positional[i] if i < len(positional) else fwd.vararg
        if param in fwd.forwarded:
            work.append(arg)
        if param is not None and param == fwd.guard:
            guard = arg
    named = set(fwd.positional) | set(fwd.keyword_only)
    for kw in call.keywords:
        if kw.arg is None:  # **kwargs at the call
            if (named | {fwd.kwarg}) & fwd.forwarded:
                work.append(kw.value)
            continue
        param = kw.arg if kw.arg in named else fwd.kwarg
        if param in fwd.forwarded:
            work.append(kw.value)
        if param is not None and param == fwd.guard:
            guard = kw.value
    return work, guard


def _is_staticmethod(fn: ast.AST) -> bool:
    return any(_tail(d) == "staticmethod" for d in getattr(fn, "decorator_list", []))


def _module_functions(
    tree: ast.Module,
) -> Dict[str, Tuple[ast.AST, Optional[str]]]:
    """Top-level functions by name and methods as ``Class.method``, with the
    class name (None for a module function)."""
    out: Dict[str, Tuple[ast.AST, Optional[str]]] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out[node.name] = (node, None)
        elif isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    out[f"{node.name}.{item.name}"] = (item, node.name)
    return out


def _forwarding(
    fn: ast.AST,
    cls_name: Optional[str],
    local: Dict[str, _Forwarder],
    aliases: Dict[str, str],
) -> Optional[_Forwarder]:
    """``fn`` as a forwarder, given the forwarders known so far, or None."""
    assert isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
    a = fn.args
    positional = tuple(p.arg for p in a.posonlyargs + a.args)
    keyword_only = tuple(p.arg for p in a.kwonlyargs)
    vararg = a.vararg.arg if a.vararg else None
    kwarg = a.kwarg.arg if a.kwarg else None
    params = (
        (set(positional) | set(keyword_only) | {vararg, kwarg}) - _SELF_NAMES - {None}
    )
    forwarded: Set[str] = set()
    guards: List[Optional[str]] = []
    for call in ast.walk(fn):
        if not isinstance(call, ast.Call):
            continue
        guard_node: Optional[ast.expr] = None
        kind = _offload_kind(call, aliases)
        if kind is not None:
            work = _work_args(call)
            if kind == _GUARDED_OFFLOAD:
                guard_node = next(
                    (kw.value for kw in call.keywords if kw.arg == "guard_session"),
                    None,
                )
        else:
            inner = _resolve_forwarder(call, cls_name, local, aliases)
            if inner is None:
                continue
            # _bind returns no guard for an inner forwarder without one.
            work, guard_node = _bind(call, inner[1])
        hit = {
            n.id
            for w in work
            for n in ast.walk(w)
            if isinstance(n, ast.Name) and n.id in params
        }
        if not hit:
            continue
        forwarded |= hit
        # A guard that is not a parameter never matches in _bind, which
        # compares it with this function's own parameter names.
        guards.append(guard_node.id if isinstance(guard_node, ast.Name) else None)
    if not forwarded:
        return None
    guard = guards[0] if all(g == guards[0] for g in guards) else None
    return _Forwarder(
        positional=positional,
        keyword_only=keyword_only,
        vararg=vararg,
        kwarg=kwarg,
        forwarded=frozenset(forwarded),
        guard=guard,
        bound_method=cls_name is not None and not _is_staticmethod(fn),
    )


def _module_forwarders(
    tree: ast.Module, aliases: Dict[str, str]
) -> Dict[str, _Forwarder]:
    """Every forwarder in ``tree``, wrappers of wrappers and module-level
    aliases (``name = forwarder``) included (fixpoint)."""
    functions = _module_functions(tree)
    assigned = _module_assignments(tree)
    found: Dict[str, _Forwarder] = {}
    for _ in range(len(functions) + len(assigned) + 1):
        changed = False
        for key, (fn, cls_name) in functions.items():
            fwd = _forwarding(fn, cls_name, found, aliases)
            if fwd is not None and found.get(key) != fwd:
                found[key] = fwd
                changed = True
        for name, value in assigned.items():
            fwd = found.get(value) if isinstance(value, str) else None
            if fwd is not None and found.get(name) != fwd:
                found[name] = fwd
                changed = True
        if not changed:
            break
    return found


def _module_assignments(tree: ast.Module) -> Dict[str, Optional[str]]:
    """``name = other`` at module level: name -> other, or None when the
    value is not a bare name. The last binding wins, as at import time."""
    out: Dict[str, Optional[str]] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            target, value = node.target, node.value
        else:
            continue
        if isinstance(target, ast.Name):
            out[target.id] = value.id if isinstance(value, ast.Name) else None
    return out


_IMPORTED_FORWARDERS: Dict[str, Dict[str, _Forwarder]] = {}


def _imported_forwarders(module: str) -> Dict[str, _Forwarder]:
    """Forwarders of the ``src`` module ``module`` (dotted), cached."""
    if module not in _IMPORTED_FORWARDERS:
        _IMPORTED_FORWARDERS[module] = {}  # an import cycle sees none
        base = _SRC.parent / module.replace(".", "/")
        for candidate in (base.with_suffix(".py"), base / "__init__.py"):
            if candidate.is_file():
                tree = _tree(candidate.relative_to(_SRC).as_posix())
                _IMPORTED_FORWARDERS[module] = _module_forwarders(tree, _aliases(tree))
                break
    return _IMPORTED_FORWARDERS[module]


def _resolve_forwarder(
    call: ast.Call,
    cls_name: Optional[str],
    local: Dict[str, _Forwarder],
    aliases: Dict[str, str],
) -> Optional[_Resolved]:
    """(name, forwarder) that ``call`` lands on, or None."""
    func = call.func
    if (
        isinstance(func, ast.Attribute)
        and isinstance(func.value, ast.Name)
        and func.value.id in _SELF_NAMES
    ):
        fwd = local.get(f"{cls_name}.{func.attr}") if cls_name else None
        return (func.attr, fwd) if fwd is not None else None
    if isinstance(func, ast.Name) and func.id in local:
        return (func.id, local[func.id])
    dotted = _dotted(func, aliases) or ""
    if dotted.startswith("src."):
        module, _, name = dotted.rpartition(".")
        fwd = _imported_forwarders(module).get(name)
        if fwd is not None:
            return (name, fwd)
    return None


def _target_label(work: List[ast.expr]) -> str:
    if not work:
        return "?"
    return "lambda" if isinstance(work[0], ast.Lambda) else ast.unparse(work[0])


def session_offload_violations(tree: ast.Module, path: str) -> Dict[str, str]:
    """``{exemption key: problem}`` for each offload that takes a Session
    without the guarded helper and a ``guard_session=`` naming that Session.
    A call to a forwarder (GOO-789) is judged as the offload it wraps."""
    scopes = _scopes(tree)
    aliases = _aliases(tree)
    forwarders = _module_forwarders(tree, aliases)
    found: Dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn, cls = scopes.get(id(node), (tree, None))
        where = f"{path}:{node.lineno}"
        name = _offload_kind(node, aliases)
        if name is None:
            resolved = _resolve_forwarder(
                node, cls.name if cls else None, forwarders, aliases
            )
            if resolved is None:
                continue
            name, fwd = resolved
            work, guard_arg = _bind(node, fwd)
            touched = _touched_by(work, fn, cls, aliases)
            if not touched:
                continue
            guard = ast.unparse(guard_arg) if guard_arg is not None else None
            if fwd.guard is not None and guard in touched:
                continue
            key = (
                f"{path}:{getattr(fn, 'name', '<module>')}:{name}:{_target_label(work)}"
            )
            found[key] = (
                f"{where}: {name} forwards the Session {sorted(touched)} to a "
                f"thread; pass it to {_GUARDED_OFFLOAD}(..., guard_session=<it>)"
            )
            continue
        touched = _touched_sessions(node, fn, cls, aliases)
        if not touched:
            continue
        key = f"{path}:{getattr(fn, 'name', '<module>')}:{name}:{_target_label(_offload_args(node))}"
        if name in _RAW_OFFLOADS:
            found[key] = (
                f"{where}: {name} hands the Session {sorted(touched)} to a "
                f"thread; use {_GUARDED_OFFLOAD}(..., guard_session=<it>)"
            )
            continue
        guards = [kw.value for kw in node.keywords if kw.arg == "guard_session"]
        guard = ast.unparse(guards[0]) if guards else None
        if guard not in touched:
            found[key] = (
                f"{where}: {_GUARDED_OFFLOAD} uses the Session {sorted(touched)} "
                f"but guard_session= is {guard or 'missing'}"
            )
    return found


def _all_session_offload_violations() -> Dict[str, str]:
    found: Dict[str, str] = {}
    for file in sorted(_SRC.rglob("*.py")):
        rel = file.relative_to(_SRC).as_posix()
        found.update(session_offload_violations(_tree(rel), rel))
    return found


def test_every_offload_that_takes_a_session_is_guarded() -> None:
    found = _all_session_offload_violations()
    unexempted = [
        msg for key, msg in found.items() if key not in SESSION_OFFLOAD_EXEMPTIONS
    ]
    assert not unexempted, "\n".join(unexempted)


def test_session_offload_exemptions_are_live_and_explained() -> None:
    found = _all_session_offload_violations()
    for key, reason in SESSION_OFFLOAD_EXEMPTIONS.items():
        assert reason.strip(), f"exemption {key} has no reason"
        assert key in found, f"exemption {key} no longer matches a call; drop it"


_PLANTED = """
import asyncio
import functools
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.thread_offload import to_thread_finish_on_cancel


async def raw_lambda(db=None):
    await asyncio.to_thread(lambda: db.query(1).all())


async def raw_executor(session: Session):
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, functools.partial(work, session))


async def raw_kwarg(db=None):
    await run_in_threadpool(service.search, q=1, db=db)


async def raw_closure(db=None):
    def _load():
        return db.execute("select 1")

    await asyncio.to_thread(_load)


async def raw_local_session():
    s = SessionLocal()
    await asyncio.to_thread(s.commit)


class Repo:
    async def raw_self_method(self):
        await asyncio.to_thread(self._load)

    def _load(self):
        return self.db.query(1)


async def guarded_without_guard(db=None):
    await to_thread_finish_on_cancel(lambda: db.commit())


async def guarded_wrong_guard(db=None, other=None):
    await to_thread_finish_on_cancel(lambda: db.commit(), guard_session=other)


async def ok_guarded_any_name(sess: Session):
    await to_thread_finish_on_cancel(lambda: sess.commit(), guard_session=sess)


async def ok_async_session_awaited_on_loop(db: AsyncSession):
    await asyncio.to_thread(render, await build(db))


async def ok_db_only_awaited_on_loop(db=None):
    await asyncio.to_thread(render, await build(db))


async def ok_no_session():
    await asyncio.to_thread(render, 1)
"""


def test_a_new_unguarded_session_offload_fails_the_guard() -> None:
    found = session_offload_violations(ast.parse(_PLANTED), "planted.py")
    functions = sorted({key.split(":")[1] for key in found})
    assert functions == [
        "guarded_without_guard",
        "guarded_wrong_guard",
        "raw_closure",
        "raw_executor",
        "raw_kwarg",
        "raw_lambda",
        "raw_local_session",
        "raw_self_method",
    ], found


# ---- GOO-783: forms the name-only match missed ----
#
# Each planted module hands a sync Session to a thread in a form the GOO-766
# scan did not see. ``planted`` must be the one violation; ``ok_*`` functions
# in the same module must stay clean.

_EVASIONS: Dict[str, str] = {
    "aliased_to_thread": """
from asyncio import to_thread as off_loop

async def planted(db=None):
    await off_loop(lambda: db.commit())
""",
    "aliased_module": """
import asyncio as aio

async def planted(db=None):
    await aio.to_thread(db.commit)
""",
    "aliased_guarded_helper": """
from src.core.thread_offload import to_thread_finish_on_cancel as guarded

async def planted(db=None):
    await guarded(lambda: db.commit())

async def ok_guarded(db=None):
    await guarded(lambda: db.commit(), guard_session=db)
""",
    "anyio_run_sync": """
import anyio

async def planted(db=None):
    await anyio.to_thread.run_sync(lambda: db.commit())
""",
    "anyio_run_sync_from_import": """
from anyio import to_thread

async def planted(db=None):
    await to_thread.run_sync(db.commit)
""",
    "anyio_run_sync_aliased": """
from anyio.to_thread import run_sync as in_thread

async def planted(db=None):
    await in_thread(db.commit)

def ok_from_thread(db=None):
    # anyio.from_thread.run_sync runs on the loop, not in a worker thread.
    from anyio import from_thread
    from_thread.run_sync(lambda: db.commit())
""",
    "pool_submit": """
from concurrent.futures import ThreadPoolExecutor

pool = ThreadPoolExecutor(2)

async def planted(db=None):
    pool.submit(work, db)

async def ok_submit_without_session():
    pool.submit(render, 1)
""",
    "partial_bound_to_a_name": """
import asyncio
from functools import partial

async def planted(db=None):
    job = partial(work, db)
    await asyncio.to_thread(job)
""",
    "partial_chained_through_names": """
import asyncio
from functools import partial as bind

async def planted(db=None):
    inner = bind(work, db)
    outer = bind(inner, 1)
    await asyncio.to_thread(outer)

async def ok_awaited_result(db=None):
    rows = await load(db)
    await asyncio.to_thread(render, rows)
""",
    "unannotated_depends_get_db_sync": """
import asyncio
from fastapi import Depends
from src.core.database import get_db_sync

async def planted(conn=Depends(get_db_sync)):
    await asyncio.to_thread(conn.commit)

async def ok_async_dependency(conn=Depends(get_db)):
    await asyncio.to_thread(render, await build(conn))
""",
    "unannotated_depends_aliased": """
import asyncio
from fastapi import Depends
from src.core.database import get_db_sync as sync_db_dep

async def planted(conn=Depends(sync_db_dep)):
    await asyncio.to_thread(lambda: conn.commit())
""",
    # GOO-789: project wrappers that forward their callable to a thread.
    "local_async_wrapper": """
import asyncio

async def _offload(fn, *a):
    return await asyncio.to_thread(fn, *a)

async def planted(db=None):
    await _offload(db.commit)

async def ok_no_session():
    await _offload(render, 1)

async def ok_awaited_on_loop(db=None):
    await _offload(render, await build(db))
""",
    "wrapper_vararg": """
import asyncio

async def _offload(fn, *a):
    return await asyncio.to_thread(fn, *a)

async def planted(db=None):
    await _offload(work, db)
""",
    "wrapper_kwargs": """
import asyncio

async def _offload(fn, **kw):
    return await asyncio.to_thread(fn, **kw)

async def planted(db=None):
    await _offload(work, session=db)
""",
    "wrapper_star_args_at_the_call": """
import asyncio

async def _offload(fn, *a):
    return await asyncio.to_thread(fn, *a)

async def planted(db=None):
    await _offload(work, *[db])
""",
    "wrapper_star_kwargs_at_the_call": """
import asyncio

async def _offload(fn, **kw):
    return await asyncio.to_thread(fn, **kw)

async def planted(db=None):
    await _offload(work, **{"session": db})
""",
    "module_level_alias": """
import asyncio

async def _offload(fn, *a):
    return await asyncio.to_thread(fn, *a)

offload = _offload
offload_again = offload

async def planted(db=None):
    await offload(db.commit)
    await offload_again(db.commit)

async def ok_no_session():
    await offload(render, 1)
""",
    "alias_last_binding_wins": """
import asyncio

def _onloop(fn):
    return fn()

async def _offload(fn, *a):
    return await asyncio.to_thread(fn, *a)

x = _onloop
x = _offload
y = _offload
y = None
z = _offload
z = _onloop

async def planted(db=None):
    await x(db.commit)

async def ok_rebound_to_none(db=None):
    y(db.commit)

async def ok_rebound_on_the_loop(db=None):
    z(db.commit)
""",
    "imported_src_alias": """
from src.services.integrations.arxiv_fulltext import run_in_subprocess

async def planted(db=None):
    await run_in_subprocess(db.commit)
""",
    "wrapper_defined_before_its_wrapped": """
import asyncio

async def _outer(job):
    return await _inner(job)

async def _inner(fn, *a):
    return await asyncio.to_thread(fn, *a)

async def planted(db=None):
    await _outer(db.commit)
""",
    "wrapper_with_two_different_guards": """
from src.core.thread_offload import to_thread_finish_on_cancel

async def _two(fn, first, second):
    await to_thread_finish_on_cancel(fn, guard_session=first)
    await to_thread_finish_on_cancel(fn, guard_session=second)

async def planted(db=None, other=None):
    await _two(lambda: db.commit(), db, other)
""",
    "wrapper_of_a_wrapper": """
import asyncio

async def _inner(fn, *a):
    return await asyncio.get_running_loop().run_in_executor(None, fn, *a)

async def _outer(job):
    return await _inner(job)

async def planted(db=None):
    await _outer(lambda: db.commit())
""",
    "self_method_wrapper": """
import asyncio

class Service:
    async def _offload(self, func, *args, **kwargs):
        return await asyncio.to_thread(func, *args, **kwargs)

    async def planted(self):
        await self._offload(self.db.commit)

    async def ok_no_session(self):
        await self._offload(self.kg.create_entity, 1)
""",
    "guarded_wrapper": """
from src.core.thread_offload import to_thread_finish_on_cancel

async def _guarded(fn, session):
    return await to_thread_finish_on_cancel(fn, guard_session=session)

async def planted(db=None, other=None):
    await _guarded(lambda: db.commit(), other)

async def ok_guards_its_session(db=None):
    await _guarded(lambda: db.commit(), db)
""",
    "wrapper_without_its_guard": """
from src.core.thread_offload import to_thread_finish_on_cancel

async def _half_guarded(fn):
    return await to_thread_finish_on_cancel(fn)

async def planted(db=None):
    await _half_guarded(db.commit)
""",
    "imported_src_wrapper": """
from src.services.integrations.arxiv_fulltext import _run_in_subprocess as sub

async def planted(db=None):
    await sub(db.commit)

async def ok_runs_on_the_loop(db=None):
    async def run(fn):
        return fn()

    await run(db.commit)
""",
}


@pytest.mark.parametrize("form", sorted(_EVASIONS))
def test_an_evasive_session_offload_fails_the_guard(form: str) -> None:
    found = session_offload_violations(ast.parse(_EVASIONS[form]), f"{form}.py")
    functions = sorted({key.split(":")[1] for key in found})
    assert functions == ["planted"], found


def test_the_real_forwarders_are_found() -> None:
    """Guards the GOO-789 resolution itself: the two forwarders in src today
    must be seen, or a call through them would go unchecked silently."""
    fulltext = _imported_forwarders("src.services.integrations.arxiv_fulltext")
    assert "_run_in_subprocess" in fulltext, sorted(fulltext)
    assert fulltext["_run_in_subprocess"].forwarded == {"fn", "args"}
    # The public alias arxiv_service imports (``run_in_subprocess =
    # _run_in_subprocess``) resolves to the same forwarder.
    assert fulltext.get("run_in_subprocess") == fulltext["_run_in_subprocess"]
    kg = _imported_forwarders("src.services.arxiv.arxiv_kg_integration")
    offload = [key for key in kg if key.endswith("._offload")]
    assert offload, sorted(kg)
    assert kg[offload[0]].forwarded == {"func", "args", "kwargs"}


_KNOWN_LIMITS = """
import asyncio

async def _offload(fn, *a):
    return await asyncio.to_thread(fn, *a)

async def through_another_object(svc, db=None):
    await svc._offload(db.commit)

async def through_a_variable(db=None):
    run = _offload
    await run(db.commit)

async def through_a_nested_def(db=None):
    async def _nested(fn):
        return await asyncio.to_thread(fn)

    await _nested(db.commit)

class Stored:
    def __init__(self, fn):
        self._job = fn

    async def run(self):
        return await asyncio.to_thread(self._job)

async def through_a_stored_callable(db=None):
    await Stored(db.commit).run()
"""


def test_known_limits_of_forwarder_resolution() -> None:
    """Pins what GOO-789 does not resolve (see the comment above
    ``_Forwarder``). If one of these starts failing the guard, the resolution
    grew: move the form into ``_EVASIONS`` and update that comment."""
    found = session_offload_violations(ast.parse(_KNOWN_LIMITS), "limits.py")
    assert found == {}, found
