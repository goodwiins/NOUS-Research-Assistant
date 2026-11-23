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
"""

import ast
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Set, Tuple

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

_RAW_OFFLOADS = frozenset({"to_thread", "run_in_executor", "run_in_threadpool"})
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
}


def _annotation(arg: ast.arg) -> str:
    return ast.unparse(arg.annotation) if arg.annotation is not None else ""


def _sync_session_names(fn: ast.AST) -> Set[str]:
    """Names that hold a sync Session inside ``fn``."""
    names = set(_SESSION_NAMES)
    if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
        for arg in fn.args.posonlyargs + fn.args.args + fn.args.kwonlyargs:
            ann = _annotation(arg)
            if "AsyncSession" in ann:
                names.discard(arg.arg)
            elif "Session" in ann:
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


def _touched_sessions(
    call: ast.Call, fn: ast.AST, cls: Optional[ast.ClassDef]
) -> Set[str]:
    names = _sync_session_names(fn)
    args = _offload_args(call)
    work = args + [kw.value for kw in call.keywords if kw.arg not in _HELPER_KWARGS]
    touched: Set[str] = set()
    for node in work:
        touched |= _refs(node, names)
    target = args[0] if args else None
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


def session_offload_violations(tree: ast.Module, path: str) -> Dict[str, str]:
    """``{exemption key: problem}`` for each offload that takes a Session
    without the guarded helper and a ``guard_session=`` naming that Session."""
    scopes = _scopes(tree)
    found: Dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _tail(node.func)
        if name not in _RAW_OFFLOADS and name != _GUARDED_OFFLOAD:
            continue
        fn, cls = scopes.get(id(node), (tree, None))
        touched = _touched_sessions(node, fn, cls)
        if not touched:
            continue
        args = _offload_args(node)
        target = "?" if not args else ast.unparse(args[0])
        if args and isinstance(args[0], ast.Lambda):
            target = "lambda"
        key = f"{path}:{getattr(fn, 'name', '<module>')}:{name}:{target}"
        where = f"{path}:{node.lineno}"
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
