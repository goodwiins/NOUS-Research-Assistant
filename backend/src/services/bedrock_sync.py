"""Queued write path for the shared Bedrock knowledge base.

Retrieval (``bedrock_retrieval.py``) reads one shared KB whose S3 data source
holds ``{prefix}{organization_id}/{document_id}.{ext}`` plus a sidecar
``{same key}.metadata.json`` carrying the two attributes every read filters on.

Writes happen in two steps so that a document is only ever marked indexed by an
ingestion job that could have seen its file:

1. **Stage** (ingest path, no Bedrock call): upload the text and sidecar, then
   set ``do_kb_sync_status='pending'`` with a fresh ``stage_token`` and no job
   claim. Upload comes first, so a pending row always has its file in S3.
2. **Tick** (``src.tasks.bedrock_sync_tasks``, one runner under an advisory
   lock): settle the job that claimed documents, then select the unclaimed
   pending ids, start one ingestion job and stamp its id on exactly those ids,
   provided each row's ``stage_token`` is unchanged. A row staged after the
   selection keeps its new token and waits for the next job.

Deleted documents are re-marked pending inside the soft-delete transaction
(``mark_deleted``); the tick removes their objects before starting the job, so
the data source's DELETE policy drops their vectors.

A failed staging upload leaves the row ``failed`` and counts the failure in
``stage_failures``. The tick retries the upload, a few rows per run, until
``MAX_ATTEMPTS`` failures; after that recovery is the next ingest of the
document or ``scripts/backfill_bedrock_kb``. Until a retry succeeds, scoped
retrieval answers from keyword search.

Enabled only when ``BEDROCK_KB_ID`` and ``BEDROCK_KB_DATA_SOURCE_ID`` are both
set; every entry point is a no-op otherwise.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Optional
from uuid import uuid4

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from src.core.config import settings
from src.models.document import Document
from src.shared.enums import SatelliteSyncStatus

logger = logging.getLogger(__name__)

META_KEY = "bedrock_sync"
MAX_ATTEMPTS = 3
STAGE_RETRY_BATCH = 10  # failed uploads retried per tick
CONTENT_EXTENSIONS = ("txt", "pdf", "png")
SIDECAR_SUFFIX = ".metadata.json"
BEDROCK_REGION = "us-east-1"  # matches bedrock_retrieval.retrieve_chunks
IN_FLIGHT_STATUSES = frozenset({"STARTING", "IN_PROGRESS", "STOPPING"})

_PENDING = SatelliteSyncStatus.PENDING.value
_COMPLETED = SatelliteSyncStatus.COMPLETED.value
_FAILED = SatelliteSyncStatus.FAILED.value


# ``Document`` declares legacy ``Column(...)`` attributes, which mypy types as
# ``Column[T]`` rather than ``T`` on instances; rows are handled as ``Any``.
DocumentRow = Any


class StageOutcome(str, Enum):
    STAGED = "staged"
    SKIPPED = "skipped"
    FAILED = "failed"


def bedrock_sync_enabled() -> bool:
    kb_id = getattr(settings, "BEDROCK_KB_ID", "")
    data_source_id = getattr(settings, "BEDROCK_KB_DATA_SOURCE_ID", "")
    return (
        isinstance(kb_id, str)
        and bool(kb_id)
        and isinstance(data_source_id, str)
        and bool(data_source_id)
    )


def is_indexed(sync_status: Any, document_metadata: Any) -> bool:
    """True only when a Bedrock ingestion job settled this document.

    ``do_kb_sync_status`` is shared with the retired DigitalOcean KB, so a
    bare ``completed`` can predate Bedrock and say nothing about this index.
    Rows this module staged always carry a ``stage_token``.
    """
    state = (document_metadata or {}).get(META_KEY) or {}
    return sync_status == _COMPLETED and bool(state.get("stage_token"))


def _token_column() -> Any:
    return Document.document_metadata[META_KEY]["stage_token"].as_string()


def not_indexed_clause() -> Any:
    """SQL twin of ``not is_indexed(...)`` (NULL-safe)."""
    return or_(
        Document.do_kb_sync_status.is_distinct_from(_COMPLETED),
        _token_column().is_(None),
    )


def _prefix() -> str:
    prefix = settings.BEDROCK_KB_S3_PREFIX or ""
    return prefix if not prefix or prefix.endswith("/") else f"{prefix}/"


def content_key(organization_id: Any, document_id: Any, ext: str = "txt") -> str:
    return f"{_prefix()}{organization_id}/{document_id}.{ext}"


def sidecar_key(key: str) -> str:
    return f"{key}{SIDECAR_SUFFIX}"


def sidecar_body(organization_id: Any, document_id: Any) -> bytes:
    return json.dumps(
        {
            "metadataAttributes": {
                "organization_id": str(organization_id),
                "document_id": str(document_id),
            }
        }
    ).encode("utf-8")


def _delete_or_raise(helper: Any, key: str) -> None:
    # S3 DeleteObject succeeds for an absent key, so no ListBucket is needed.
    if not helper.delete_file(key):
        raise RuntimeError(f"S3 delete failed for {key}")


def upload_document_objects(
    helper: Any, organization_id: Any, document_id: Any, text: str
) -> None:
    """Write ``.txt`` + sidecar, then remove any other-extension content.

    Legacy documents carry a ``.pdf``/``.png`` object under the same stem;
    leaving it would index the document twice. Blocking (boto3).
    """
    key = content_key(organization_id, document_id)
    helper.upload_file(key, text.encode("utf-8"), "text/plain; charset=utf-8")
    helper.upload_file(
        sidecar_key(key),
        sidecar_body(organization_id, document_id),
        "application/json",
    )
    for ext in CONTENT_EXTENSIONS:
        if ext == "txt":
            continue
        other = content_key(organization_id, document_id, ext)
        _delete_or_raise(helper, other)
        _delete_or_raise(helper, sidecar_key(other))


def delete_document_objects(
    helper: Any, organization_id: Any, document_id: Any
) -> None:
    """Remove every candidate content object and sidecar. Blocking (boto3)."""
    for ext in CONTENT_EXTENSIONS:
        key = content_key(organization_id, document_id, ext)
        _delete_or_raise(helper, key)
        _delete_or_raise(helper, sidecar_key(key))


def _state(document: DocumentRow) -> dict[str, Any]:
    return dict((document.document_metadata or {}).get(META_KEY) or {})


def _write_state(document: DocumentRow, state: dict[str, Any]) -> None:
    # Reassign the whole dict: plain JSON columns do not track in-place edits.
    document.document_metadata = {**(document.document_metadata or {}), META_KEY: state}


def _mark_pending(document: DocumentRow) -> None:
    """Pending, unclaimed, fresh token and attempt budget."""
    _write_state(
        document,
        {
            "stage_token": uuid4().hex,
            "staged_at": datetime.now(timezone.utc).isoformat(),
            "attempts": 0,
        },
    )
    document.do_kb_sync_status = _PENDING


def mark_deleted(document: DocumentRow) -> None:
    """Queue a deleted document's vector removal (caller owns the commit).

    Called inside the soft-delete transaction, so the intent is durable with
    the delete itself and no remote call can block the user's delete.
    """
    if bedrock_sync_enabled():
        _mark_pending(document)


def _skip_reason(document: DocumentRow) -> Optional[str]:
    if document.is_deleted:
        return "soft_deleted"
    if not document.content_text:
        return "no_content_text"
    return None


def _log_skip(document: DocumentRow, reason: str) -> None:
    logger.info(
        "bedrock_kb stage skipped",
        extra={"document_id": str(document.id), "reason": reason},
    )


_FAILURE_RECHECK = ["is_deleted", "document_metadata", "do_kb_sync_status"]


def _record_stage_failure(document: DocumentRow) -> StageOutcome:
    """Mark a failed upload. ``document`` must be freshly locked and reloaded.

    A soft delete can commit while the upload runs. Its pending removal must
    survive: nothing else would ever take the document's vectors out.
    """
    logger.warning(
        "bedrock_kb stage upload failed",
        extra={"document_id": str(document.id)},
    )
    if not document.is_deleted:
        state = _state(document)
        state["stage_failures"] = int(state.get("stage_failures") or 0) + 1
        _write_state(document, state)
        document.do_kb_sync_status = _FAILED
    return StageOutcome.FAILED


def stage_document_sync(db: Session, document: DocumentRow) -> StageOutcome:
    """Stage from a sync session. Mutates ``document``; caller commits."""
    if not bedrock_sync_enabled():
        return StageOutcome.SKIPPED
    db.flush()
    db.refresh(document, attribute_names=["is_deleted", "document_metadata"])
    if reason := _skip_reason(document):
        _log_skip(document, reason)
        return StageOutcome.SKIPPED
    try:
        from src.core.s3_client import S3StorageHelper

        upload_document_objects(
            S3StorageHelper(),
            document.organization_id,
            document.id,
            document.content_text,
        )
    except Exception:  # noqa: BLE001 - a KB outage must never fail ingestion
        logger.warning("bedrock_kb stage upload error", exc_info=True)
        db.refresh(document, attribute_names=_FAILURE_RECHECK, with_for_update=True)
        return _record_stage_failure(document)
    _mark_pending(document)
    return StageOutcome.STAGED


async def stage_document(session: AsyncSession, document: DocumentRow) -> StageOutcome:
    """Stage from an async session. Mutates ``document``; caller commits."""
    if not bedrock_sync_enabled():
        return StageOutcome.SKIPPED
    await session.flush()
    await session.refresh(document, attribute_names=["is_deleted", "document_metadata"])
    if reason := _skip_reason(document):
        _log_skip(document, reason)
        return StageOutcome.SKIPPED
    try:
        from src.core.s3_client import S3StorageHelper

        await asyncio.to_thread(
            upload_document_objects,
            S3StorageHelper(),
            document.organization_id,
            document.id,
            document.content_text,
        )
    except Exception:  # noqa: BLE001 - a KB outage must never fail ingestion
        logger.warning("bedrock_kb stage upload error", exc_info=True)
        await session.refresh(
            document, attribute_names=_FAILURE_RECHECK, with_for_update=True
        )
        return _record_stage_failure(document)
    _mark_pending(document)
    return StageOutcome.STAGED


# --------------------------------------------------------------------------
# Queue tick (single runner; the Celery task holds the advisory lock).
# Blocking boto3 calls run in a worker thread so the loop is never stalled.
# --------------------------------------------------------------------------


def get_bedrock_agent_client() -> Any:
    import boto3

    return boto3.client("bedrock-agent", region_name=BEDROCK_REGION)


def _error_code(exc: Exception) -> str:
    response = getattr(exc, "response", None) or {}
    return str((response.get("Error") or {}).get("Code") or "")


def _job_column() -> Any:
    return Document.document_metadata[META_KEY]["job_id"].as_string()


def _kb_ids() -> dict[str, str]:
    return {
        "knowledgeBaseId": settings.BEDROCK_KB_ID,
        "dataSourceId": settings.BEDROCK_KB_DATA_SOURCE_ID,
    }


async def _lock_row(db: AsyncSession, document_id: Any) -> Optional[DocumentRow]:
    return (
        await db.execute(
            select(Document)
            .where(Document.id == document_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


def _record_failure(document: DocumentRow, summary: dict[str, Any]) -> None:
    state = _state(document)
    state.pop("job_id", None)
    state["attempts"] = int(state.get("attempts") or 0) + 1
    _write_state(document, state)
    if state["attempts"] >= MAX_ATTEMPTS:
        document.do_kb_sync_status = _FAILED
        summary["failed"] += 1
    else:
        summary["retried"] += 1


def _record_completed(document: DocumentRow, summary: dict[str, Any]) -> None:
    state = _state(document)
    state.pop("job_id", None)
    state["attempts"] = 0
    _write_state(document, state)
    document.do_kb_sync_status = _COMPLETED
    document.do_kb_indexed_at = datetime.now(timezone.utc)
    summary["completed"] += 1


async def _settle_job(
    db: AsyncSession,
    job_id: str,
    document_ids: list[Any],
    job: dict[str, Any],
    summary: dict[str, Any],
) -> None:
    complete = job.get("status") == "COMPLETE"
    reasons = [str(r) for r in (job.get("failureReasons") or [])]
    claimed: list[tuple[DocumentRow, bool]] = []
    for document_id in document_ids:
        document = await _lock_row(db, document_id)
        # A restage or delete since the claim cleared it: not this job's row.
        if (
            document is None
            or document.do_kb_sync_status != _PENDING
            or _state(document).get("job_id") != job_id
        ):
            continue
        key = content_key(document.organization_id, document.id)
        claimed.append((document, any(key in reason for reason in reasons)))

    # COMPLETE does not mean every document was ingested: the job also reports
    # a failed-document count, and ``failureReasons`` need not name each one.
    # When more documents failed than we can name, none of the live claimed
    # documents is provably indexed, so they keep their retry budget.
    failed_count = int(
        (job.get("statistics") or {}).get("numberOfDocumentsFailed") or 0
    )
    unattributed = failed_count > sum(1 for _, named in claimed if named)
    if unattributed:
        summary["unattributed_failures"] = failed_count
    for document, named in claimed:
        # A deleted document has no object left to fail ingestion.
        suspect = named or (unattributed and not document.is_deleted)
        if complete and not suspect:
            _record_completed(document, summary)
        else:
            _record_failure(document, summary)
    await db.commit()


async def _settle_claimed(
    db: AsyncSession, client: Any, summary: dict[str, Any]
) -> bool:
    """Settle claimed documents. Returns True while a claimed job still runs."""
    rows = (
        await db.execute(
            select(Document.id, _job_column()).where(
                Document.do_kb_sync_status == _PENDING, _job_column().is_not(None)
            )
        )
    ).all()
    await db.rollback()  # no snapshot held across the remote calls
    by_job: dict[str, list[Any]] = defaultdict(list)
    for document_id, job_id in rows:
        by_job[str(job_id)].append(document_id)
    for job_id, document_ids in by_job.items():
        try:
            response = await asyncio.to_thread(
                client.get_ingestion_job, ingestionJobId=job_id, **_kb_ids()
            )
            job = response["ingestionJob"]
        except Exception as exc:  # noqa: BLE001
            if _error_code(exc) != "ResourceNotFoundException":
                logger.warning("bedrock_kb get_ingestion_job failed", exc_info=True)
                return True  # unknown state: do not start another job
            job = {"status": "FAILED"}
        if job.get("status") in IN_FLIGHT_STATUSES:
            summary["in_flight_job"] = job_id
            return True
        await _settle_job(db, job_id, document_ids, job, summary)
    return False


def _stage_failures_column() -> Any:
    return Document.document_metadata[META_KEY]["stage_failures"].as_integer()


async def _retry_failed_stages(
    db: AsyncSession, helper_factory: Callable[[], Any], summary: dict[str, Any]
) -> None:
    """Re-upload documents whose staging upload failed, within the budget.

    Each row is locked for its own upload. An ingest that changed the row
    holds that lock until it commits, so this never uploads text older than
    the committed row, and a row restaged meanwhile is no longer ``failed``.
    """
    document_ids = (
        (
            await db.execute(
                select(Document.id)
                .where(
                    Document.do_kb_sync_status == _FAILED,
                    Document.is_deleted.is_(False),
                    _stage_failures_column().between(1, MAX_ATTEMPTS - 1),
                )
                .order_by(Document.id)
                .limit(STAGE_RETRY_BATCH)
            )
        )
        .scalars()
        .all()
    )
    await db.rollback()
    helper = None
    for document_id in document_ids:
        document = await _lock_row(db, document_id)
        failures = int(_state(document).get("stage_failures") or 0) if document else 0
        if (
            document is None
            or document.do_kb_sync_status != _FAILED
            or document.is_deleted
            or not document.content_text
            or not 1 <= failures < MAX_ATTEMPTS
        ):
            await db.rollback()
            continue
        try:
            helper = helper or helper_factory()
            await asyncio.to_thread(
                upload_document_objects,
                helper,
                document.organization_id,
                document.id,
                document.content_text,
            )
        except Exception:  # noqa: BLE001 - counted; retried on a later tick
            logger.warning(
                "bedrock_kb stage retry failed",
                extra={"document_id": str(document_id)},
                exc_info=True,
            )
            _record_stage_failure(document)
            summary["stage_retry_failed"] = summary.get("stage_retry_failed", 0) + 1
        else:
            _mark_pending(document)
            summary["restaged"] = summary.get("restaged", 0) + 1
        await db.commit()


async def _job_running_elsewhere(client: Any) -> bool:
    response = await asyncio.to_thread(
        client.list_ingestion_jobs,
        sortBy={"attribute": "STARTED_AT", "order": "DESCENDING"},
        maxResults=5,
        **_kb_ids(),
    )
    return any(
        job.get("status") in IN_FLIGHT_STATUSES
        for job in response.get("ingestionJobSummaries") or []
    )


async def _claim_new_job(
    db: AsyncSession,
    client: Any,
    helper_factory: Callable[[], Any],
    summary: dict[str, Any],
) -> None:
    # Select FIRST: the job will only cover files already in S3 now.
    selected = (
        await db.execute(
            select(
                Document.id,
                Document.organization_id,
                Document.is_deleted,
                _token_column(),
            ).where(
                Document.do_kb_sync_status == _PENDING,
                _job_column().is_(None),
                # A pending row without a token was never staged here (a
                # DigitalOcean-era leftover); its file is not in this prefix.
                _token_column().is_not(None),
            )
        )
    ).all()
    await db.rollback()  # no snapshot or row locks across the remote calls
    if not selected:
        return

    tokens: dict[Any, Optional[str]] = {}
    helper = None
    for document_id, organization_id, is_deleted, token in selected:
        if is_deleted:
            try:
                helper = helper or helper_factory()
                await asyncio.to_thread(
                    delete_document_objects, helper, organization_id, document_id
                )
            except Exception:  # noqa: BLE001 - retried next tick
                logger.warning(
                    "bedrock_kb delete of removed document objects failed",
                    extra={"document_id": str(document_id)},
                    exc_info=True,
                )
                continue
        tokens[document_id] = token
    if not tokens:
        return

    try:
        response = await asyncio.to_thread(
            client.start_ingestion_job, description="nous queued sync", **_kb_ids()
        )
    except Exception as exc:  # noqa: BLE001
        if _error_code(exc) == "ConflictException":
            summary["conflict"] = True
            return
        raise
    job_id = str(response["ingestionJob"]["ingestionJobId"])
    summary["started_job"] = job_id

    # Stamp by the selected ids, never by "all currently pending", and only
    # when the row was not restaged (new token) since the selection.
    for document_id, token in tokens.items():
        document = await _lock_row(db, document_id)
        if document is None or document.do_kb_sync_status != _PENDING:
            continue
        state = _state(document)
        if state.get("job_id") or state.get("stage_token") != token:
            continue
        state["job_id"] = job_id
        state["claimed_at"] = datetime.now(timezone.utc).isoformat()
        _write_state(document, state)
        summary["claimed"] += 1
    await db.commit()


async def run_sync_tick(
    db: AsyncSession,
    client: Any,
    *,
    helper_factory: Optional[Callable[[], Any]] = None,
) -> dict[str, Any]:
    """One queue step. The caller guarantees a single concurrent runner."""
    summary: dict[str, Any] = {
        "completed": 0,
        "retried": 0,
        "failed": 0,
        "claimed": 0,
    }
    if not bedrock_sync_enabled():
        summary["skipped"] = "disabled"
        return summary
    if await _settle_claimed(db, client, summary):
        return summary
    if await _job_running_elsewhere(client):
        summary["in_flight_job"] = "external"
        return summary
    if helper_factory is None:
        from src.core.s3_client import S3StorageHelper

        helper_factory = S3StorageHelper
    await _retry_failed_stages(db, helper_factory, summary)
    await _claim_new_job(db, client, helper_factory, summary)
    return summary
