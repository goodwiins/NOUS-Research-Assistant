"""CLI: unlink message citations whose document the thread may not show (GOO-671).

Before #2195 (GOO-631), ``POST /api/v2/messages`` stored ``citations[].document_id``
from the request without an ownership check. The message presenter falls back
to ``citation.document.title`` / ``document_type``, so a citation that names
another organization's document still shows that document whenever its thread
is reopened. #2195 stops new ones (``filter_owned_document_ids``: the document
must belong to the *citing user's* org and not be soft-deleted); this one-off
cleans up old rows.

Examples (from ``backend/``):
  python -m scripts.neutralize_cross_org_message_citations                 # dry run (default)
  python -m scripts.neutralize_cross_org_message_citations --audit-file a.ndjson
  python -m scripts.neutralize_cross_org_message_citations --apply --audit-file a.ndjson

Which orgs a citation's document may belong to:

* User-authored message whose author has an org: exactly that org, the same
  rule #2195 applies at write time. A document of the thread's own org that
  the (cross-org member) author does not belong to is unlinked.
* Assistant/system message (``user_id`` is NULL), or an author with no org:
  the citing user is unknown, so exact parity is impossible. The document may
  belong to any org that legitimately takes part in the thread: the
  workspace's org, the workspace owner's org, the thread creator's org, or the
  org of any active (not soft-deleted) workspace member. Workspace membership
  crosses orgs by design, so a cross-org member's own document is kept.
  RESIDUAL LEAK: an assistant citation to a document of one participating org
  that the requesting user could not see is kept, because the stored row
  does not say who asked. Removed members' orgs do not count.
* If none of those orgs is known, the row is counted as ``undetermined_org``
  and left alone.

What changes, per citation attached to a chat message with a ``document_id``:

* ``cross_org``: the document's org is not allowed. ``--apply`` sets
  ``document_id`` to NULL and clears ``document_title`` / ``document_type`` when
  they equal the document's own values (only that document could have
  supplied them). The client's own snippet, label and scores stay.
* ``deleted_document``: allowed org, but the document is soft-deleted (#2195
  drops those too). Only ``document_id`` is cleared; the copied title is the
  thread's own org's data and stays.

Same-org live citations, citations without a document, and citations not
attached to a message are never touched. (``citations.document_id`` is
``ON DELETE SET NULL``, so a dangling id cannot exist.)

Audit trail: every affected row is written as one NDJSON record
(``"event": "citation"``) in BOTH modes, to ``--audit-file`` (appended) or
stdout: citation id, old document id, old document type when it is cleared,
document/thread/author org, reason, fields cleared, and whether it was
applied. No titles or snippets. A cleared title equals the document's own
title, so ``old_document_id`` is enough to restore it. With ``--apply``,
records are written only after their batch commits. The last stdout line is
the ``"event": "summary"`` counts: dry runs report ``would_unlink`` /
``would_clear_fields``; ``unlinked`` / ``copied_fields_cleared`` count rows
actually written. The redacted target database is printed to stderr first.

Idempotent: unlinked rows have no ``document_id`` and drop out of the next
scan, so a rerun reports ``would_unlink: 0``. ``--apply`` commits per batch.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from collections import defaultdict
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple, cast
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from src.core.url_redaction import redact_url
from src.models.chat_message import ChatMessage
from src.models.citation import Citation
from src.models.conversation import Conversation
from src.models.document import Document
from src.models.thread import Thread
from src.models.user import User
from src.models.workspace import Workspace, WorkspaceMember

logger = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE = 500

AuditSink = Callable[[Dict[str, Any]], None]

_Owner = aliased(User, name="owner")
_Creator = aliased(User, name="creator")
_Author = aliased(User, name="author")


def _type_value(document_type: Any) -> Optional[str]:
    value = getattr(document_type, "value", document_type)
    return None if value is None else str(value)


def _str(value: Optional[UUID]) -> Optional[str]:
    return None if value is None else str(value)


def _batch_query(after: Optional[UUID], batch_size: int) -> Any:
    stmt = (
        select(
            Citation.id,
            Citation.document_id,
            Citation.document_title,
            Citation.document_type,
            Document.organization_id.label("doc_org"),
            Document.is_deleted.label("doc_deleted"),
            Document.title.label("doc_title"),
            Document.document_type.label("doc_type"),
            Workspace.id.label("workspace_id"),
            Workspace.organization_id.label("workspace_org"),
            _Owner.organization_id.label("owner_org"),
            _Creator.organization_id.label("creator_org"),
            ChatMessage.user_id.label("author_id"),
            _Author.organization_id.label("author_org"),
        )
        .join(Document, Document.id == Citation.document_id)
        .join(ChatMessage, ChatMessage.id == Citation.message_id)
        .join(Thread, Thread.id == ChatMessage.thread_id)
        .join(Conversation, Conversation.id == Thread.conversation_id)
        .join(Workspace, Workspace.id == Conversation.workspace_id)
        .outerjoin(_Owner, _Owner.id == Workspace.owner_id)
        .outerjoin(_Creator, _Creator.id == Thread.created_by_id)
        .outerjoin(_Author, _Author.id == ChatMessage.user_id)
        .order_by(Citation.id)
        .limit(batch_size)
    )
    if after is not None:
        stmt = stmt.where(Citation.id > after)
    return stmt


async def _member_orgs(
    db: AsyncSession, workspace_ids: Set[UUID]
) -> Dict[UUID, Set[UUID]]:
    """Orgs of each workspace's active members (soft-deleted members excluded)."""
    result = await db.execute(
        select(WorkspaceMember.workspace_id, User.organization_id)
        .join(User, User.id == WorkspaceMember.user_id)
        .where(
            WorkspaceMember.workspace_id.in_(workspace_ids),
            WorkspaceMember.is_deleted == False,  # noqa: E712
            User.organization_id.is_not(None),
        )
    )
    orgs: Dict[UUID, Set[UUID]] = defaultdict(set)
    for workspace_id, organization_id in result.all():
        orgs[workspace_id].add(organization_id)
    return orgs


def _allowed_orgs(row: Any, member_orgs: Dict[UUID, Set[UUID]]) -> Set[UUID]:
    if row.author_id is not None and row.author_org is not None:
        return {row.author_org}  # exact #2195 parity
    candidates = {row.workspace_org, row.owner_org, row.creator_org}
    candidates |= member_orgs.get(row.workspace_id, set())
    return {org for org in candidates if org is not None}


def _plan(row: Any, allowed: Set[UUID]) -> Optional[Tuple[str, Dict[str, Any]]]:
    """Return (reason, column values to write), or None to leave the row alone."""
    if row.doc_org not in allowed:
        values: Dict[str, Any] = {"document_id": None}
        if row.document_title is not None and row.document_title == row.doc_title:
            values["document_title"] = None
        if row.document_type is not None and row.document_type == _type_value(
            row.doc_type
        ):
            values["document_type"] = None
        return "cross_org", values
    if row.doc_deleted:
        return "deleted_document", {"document_id": None}
    return None


async def neutralize(
    factory: async_sessionmaker[AsyncSession],
    *,
    apply: bool,
    batch_size: int = DEFAULT_BATCH_SIZE,
    audit: Optional[AuditSink] = None,
) -> Dict[str, int]:
    """Scan message citations and (with ``apply``) unlink the disallowed ones."""
    counts = {
        "scanned": 0,
        "cross_org": 0,
        "deleted_document": 0,
        "undetermined_org": 0,
        "would_unlink": 0,
        "would_clear_fields": 0,
        "unlinked": 0,
        "copied_fields_cleared": 0,
    }
    after: Optional[UUID] = None
    while True:
        records: List[Dict[str, Any]] = []
        async with factory() as db:
            rows = (await db.execute(_batch_query(after, batch_size))).all()
            if not rows:
                break
            after = rows[-1].id
            member_orgs = await _member_orgs(db, {row.workspace_id for row in rows})
            for row in rows:
                counts["scanned"] += 1
                allowed = _allowed_orgs(row, member_orgs)
                if not allowed:
                    counts["undetermined_org"] += 1
                    continue
                planned = _plan(row, allowed)
                if planned is None:
                    continue
                reason, values = planned
                cleared = [key for key in values if key != "document_id"]
                counts[reason] += 1
                counts["would_unlink"] += 1
                if cleared:
                    counts["would_clear_fields"] += 1
                applied = False
                if apply:
                    # Guard on the old document_id so a concurrent edit is not
                    # overwritten; a no-op update is simply not counted.
                    result = cast(
                        CursorResult[Any],
                        await db.execute(
                            update(Citation)
                            .where(
                                Citation.id == row.id,
                                Citation.document_id == row.document_id,
                            )
                            .values(**values)
                            .execution_options(synchronize_session=False)
                        ),
                    )
                    applied = bool(result.rowcount)
                    if applied:
                        counts["unlinked"] += 1
                        if cleared:
                            counts["copied_fields_cleared"] += 1
                records.append(
                    {
                        "event": "citation",
                        "mode": "apply" if apply else "dry-run",
                        "applied": applied,
                        "reason": reason,
                        "citation_id": str(row.id),
                        "old_document_id": str(row.document_id),
                        "old_document_type": (
                            row.document_type if "document_type" in cleared else None
                        ),
                        "fields_cleared": cleared,
                        "doc_org": _str(row.doc_org),
                        "thread_org": _str(row.workspace_org or row.owner_org),
                        "author_org": _str(row.author_org),
                    }
                )
            if apply:
                await db.commit()
        # Emit only after the batch's commit, so "applied" is never a lie.
        if audit is not None:
            for record in records:
                audit(record)
        logger.info("neutralize_cross_org_message_citations progress: %s", counts)
    return counts


def _open_target() -> Tuple[async_sessionmaker[AsyncSession], str]:
    from src.core.database import ASYNC_DATABASE_URL, AsyncSessionLocal

    return AsyncSessionLocal, ASYNC_DATABASE_URL


def _parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        # An abbreviation such as ``--a`` must never turn on --apply.
        allow_abbrev=False,
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write changes, committing per batch (default: dry run, no writes)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"citations per batch (default {DEFAULT_BATCH_SIZE})",
    )
    parser.add_argument(
        "--audit-file",
        help="append one NDJSON record per affected citation here (default: stdout)",
    )
    args = parser.parse_args(argv)
    if args.batch_size < 1:
        parser.error("--batch-size must be >= 1")
    return args


def main(argv: Optional[Sequence[str]] = None) -> None:
    args = _parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    mode = "apply" if args.apply else "dry-run"

    async def run() -> Dict[str, int]:
        factory, url = _open_target()
        print(f"target database ({mode}): {redact_url(url)}", file=sys.stderr)
        if args.audit_file is None:
            return await neutralize(
                factory,
                apply=args.apply,
                batch_size=args.batch_size,
                audit=lambda record: print(json.dumps(record), flush=True),
            )
        with open(args.audit_file, "a", encoding="utf-8") as sink:

            def write(record: Dict[str, Any]) -> None:
                sink.write(json.dumps(record) + "\n")
                sink.flush()

            return await neutralize(
                factory, apply=args.apply, batch_size=args.batch_size, audit=write
            )

    result = asyncio.run(run())
    print(json.dumps({"event": "summary", "mode": mode, **result}))


if __name__ == "__main__":
    main()
