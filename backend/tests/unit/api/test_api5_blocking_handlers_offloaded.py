"""GOO-643 / API-5: sync DB / broker / PDF work must not run on the event loop.

Handlers that only do blocking work are plain ``def`` (FastAPI runs them in
its threadpool). Mixed handlers push their blocking calls into a thread.
"""

import importlib
import inspect
import threading
from pathlib import Path
from typing import Any, List
from unittest.mock import MagicMock

import fitz
import pytest

pytestmark = pytest.mark.unit

SYNC_HANDLERS = {
    "src.api.evidence.router": ["get_evidence_breakdown"],
    "src.api.infrastructure.evaluation": [
        "create_batch_evaluation_job",
        "evaluate_real_time",
        "get_evaluation_job",
        "list_evaluation_jobs",
        "get_evaluation_metrics",
        "create_evaluation_comparison",
        "list_evaluation_comparisons",
        "trigger_evaluation_report",
        "list_evaluation_reports",
        "get_evaluation_report",
        "get_metrics_summary",
        "delete_evaluation_job",
    ],
    "src.api.security.encryption": [
        "encrypt_user_profile",
        "encrypt_organization_profile",
        "decrypt_data",
        "rotate_encryption_key",
        "get_encryption_status",
        "validate_encryption_integrity",
        "get_encryption_audit_logs",
    ],
    "src.api.quality.user_behavior": ["get_content_usage_analytics"],
    "src.api.search.search": [
        "get_search_suggestions",
        "rebuild_search_indexes",
        "reindex_document",
        "persist_api_key_usage_log",
    ],
    "src.api.infrastructure.workers": [
        "get_worker_status",
        "get_queue_status",
        "ping_workers",
        "get_registered_tasks",
        "shutdown_worker",
        "get_workers_health",
    ],
    "src.api.arxiv.arxiv_local": ["list_local_papers", "get_local_papers_stats"],
}


@pytest.mark.parametrize(
    ("module", "name"),
    [(m, n) for m, names in SYNC_HANDLERS.items() for n in names],
)
def test_blocking_only_handlers_are_sync(module: str, name: str) -> None:
    fn = getattr(importlib.import_module(module), name)
    assert not inspect.iscoroutinefunction(fn), f"{module}.{name} must be def"


async def test_user_behavior_service_calls_run_off_the_loop_thread() -> None:
    from src.api.quality import user_behavior

    seen: List[threading.Thread] = []

    async def _service_method(value: int) -> int:
        seen.append(threading.current_thread())
        return value * 2

    assert await user_behavior._off_loop(_service_method, 21) == 42
    assert seen and seen[0] is not threading.current_thread()


def test_local_pdf_reader_extracts_text_and_metadata(tmp_path: Path) -> None:
    from src.api.arxiv import arxiv_local

    path = tmp_path / "2401.00001.pdf"
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), "local pdf text")
    doc.save(str(path))
    doc.close()

    text, metadata = arxiv_local._read_pdf_text_and_metadata(path)

    assert "local pdf text" in text
    assert metadata["page_count"] == 1


def test_evidence_save_rolls_back_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.api.evidence import router

    def _boom(**_kwargs: Any) -> int:
        raise RuntimeError("db down")

    monkeypatch.setattr(router, "_save_stance_classifications", _boom)
    db = MagicMock()

    with pytest.raises(RuntimeError):
        router._save_and_commit(db, classifications=[])

    db.rollback.assert_called_once()
    db.commit.assert_not_called()


def test_evaluation_job_is_refreshed_after_its_last_commit() -> None:
    """The handler reads job.id/name/status/created_at on the event loop;
    the last commit expires them, so the reload must happen in the thread."""
    from src.services.evaluation.rag_evaluation_service import (
        EvaluationRequest,
        EvaluationType,
        rag_evaluation_service,
    )

    db = MagicMock()
    request = EvaluationRequest(
        name="n",
        description="d",
        evaluation_type=list(EvaluationType)[0],
        dataset=[],
        parameters={},
        user_id="anonymous",
        organization_id="org",
    )

    rag_evaluation_service._create_evaluation_job_sync(request, db)

    calls = [name for name, _args, _kwargs in db.method_calls]
    last_commit = max(i for i, name in enumerate(calls) if name == "commit")
    assert "refresh" in calls[last_commit + 1 :]
