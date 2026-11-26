"""CLI: stage existing Documents for the shared Bedrock knowledge base.

Examples:
  python -m scripts.backfill_bedrock_kb            # dry run: counts only
  python -m scripts.backfill_bedrock_kb --apply    # upload + mark pending
  python -m scripts.backfill_bedrock_kb --prune    # count stale objects
  python -m scripts.backfill_bedrock_kb --prune --apply  # remove and sync

Stages every non-deleted document that has text and has not been settled by a
Bedrock ingestion job, organization by organization. Staging
uploads ``{prefix}{org}/{doc}.txt`` plus its metadata sidecar, removes any
legacy ``.pdf``/``.png`` object for that document and marks the row
``pending``. It never starts an ingestion job: the ``bedrock-kb-sync-tick``
beat task does that, one job at a time. Re-runs are safe (staging overwrites
the same keys). Prints counts only, never titles or content. The separate
prune mode checks the configured S3 prefix against Postgres and starts an
ingestion job after applying deletions.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from dataclasses import dataclass
from typing import Any, Callable, Optional

from sqlalchemy.orm import Session

from src.models.document import Document
from src.services.bedrock_sync import (
    STAGE_WRITE_COLUMNS,
    StageOutcome,
    bedrock_sync_enabled,
    begin_stage_write,
    finish_stage_write,
    get_bedrock_agent_client,
    not_indexed_clause,
    prune_staged_objects,
    stage_document_sync,
)

logger = logging.getLogger("bedrock_kb_backfill")


@dataclass
class OrgReport:
    organization_id: str
    eligible: int = 0
    staged: int = 0
    skipped: int = 0
    failed: int = 0


def _eligible_filters() -> list[Any]:
    return [
        Document.is_deleted == False,  # noqa: E712
        Document.content_text.is_not(None),
        Document.content_text != "",
        # Not "status != completed": a completed status left by the retired
        # DigitalOcean KB says nothing about this index.
        not_indexed_clause(),
    ]


def backfill(
    session_factory: Callable[[], Session],
    *,
    apply: bool,
    stage: Callable[[Session, Document], StageOutcome] = stage_document_sync,
    out: Optional[Callable[[str], None]] = None,
) -> list[OrgReport]:
    emit = out or print
    reports: list[OrgReport] = []
    db = session_factory()
    try:
        org_ids = [
            row[0]
            for row in db.query(Document.organization_id)
            .filter(*_eligible_filters())
            .distinct()
            .order_by(Document.organization_id)
            .all()
        ]
        for org_id in org_ids:
            report = OrgReport(organization_id=str(org_id))
            document_ids = [
                row[0]
                for row in db.query(Document.id)
                .filter(Document.organization_id == org_id, *_eligible_filters())
                .order_by(Document.id)
                .all()
            ]
            report.eligible = len(document_ids)
            if apply:
                for document_id in document_ids:
                    document = db.get(Document, document_id)
                    if document is None:
                        report.skipped += 1
                        continue
                    db.refresh(
                        document,
                        attribute_names=STAGE_WRITE_COLUMNS,
                        with_for_update=True,
                    )
                    if document.is_deleted:
                        db.rollback()
                        report.skipped += 1
                        continue
                    write_token = begin_stage_write(document)
                    db.commit()  # durable before the upload starts
                    outcome = stage(db, document)
                    finish_stage_write(document, write_token)
                    db.commit()
                    if outcome is StageOutcome.STAGED:
                        report.staged += 1
                    elif outcome is StageOutcome.FAILED:
                        report.failed += 1
                    else:
                        report.skipped += 1
            emit(
                f"[org {report.organization_id}] eligible={report.eligible} "
                f"staged={report.staged} skipped={report.skipped} "
                f"failed={report.failed}"
            )
            reports.append(report)
    finally:
        db.close()
    totals = {
        key: sum(getattr(r, key) for r in reports)
        for key in ("eligible", "staged", "skipped", "failed")
    }
    mode = "apply" if apply else "dry-run"
    emit(
        f"[{mode}] orgs={len(reports)} "
        + " ".join(f"{key}={value}" for key, value in totals.items())
    )
    return reports


async def _prune(*, apply: bool) -> dict[str, Any]:
    from src.core.database import AsyncSessionLocal, async_engine
    from src.core.s3_client import S3StorageHelper
    from src.tasks.bedrock_sync_tasks import tick_lock

    async with async_engine.connect() as connection, tick_lock(connection) as acquired:
        if not acquired:
            raise RuntimeError("The Bedrock sync tick is active. Retry the prune.")
        async with AsyncSessionLocal() as db:
            return await prune_staged_objects(
                db, get_bedrock_agent_client(), S3StorageHelper(), apply=apply
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply staging or pruning (default is a dry run)",
    )
    parser.add_argument(
        "--prune",
        action="store_true",
        help="Reconcile stale S3 objects instead of staging",
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    if not bedrock_sync_enabled():
        print(
            "ERROR: set BEDROCK_KB_ID and BEDROCK_KB_DATA_SOURCE_ID first.",
            file=sys.stderr,
        )
        sys.exit(2)
    if args.prune:
        print(asyncio.run(_prune(apply=args.apply)))
        return

    from src.core.database import SessionLocal

    reports = backfill(SessionLocal, apply=args.apply)
    sys.exit(1 if any(r.failed for r in reports) else 0)


if __name__ == "__main__":
    main()
