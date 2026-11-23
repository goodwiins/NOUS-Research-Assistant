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
Session as ``guard_session=db`` (GOO-698), so a thread abandoned after the
bounded wait can neither commit on it nor have it closed under it.
"""

import ast
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Tuple

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


def _passes_db_as_guard(call: ast.Call) -> bool:
    return any(
        kw.arg == "guard_session"
        and isinstance(kw.value, ast.Name)
        and kw.value.id == "db"
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
            if _matches(arg, site.target) and not _passes_db_as_guard(call)
        ]
        assert not unguarded, (
            f"{site.target} uses the request Session but does not pass "
            f"guard_session=db (lines {unguarded})"
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
