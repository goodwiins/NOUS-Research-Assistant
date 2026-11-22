"""CLI: unlink message citations whose document the thread may not show (GOO-671).

Before #2195 (GOO-631), ``POST /api/v2/messages`` stored ``citations[].document_id``
from the request without an ownership check. The message presenter falls back
to ``citation.document.title`` / ``document_type``, so a citation that names
another organization's document still shows that document whenever its thread
is reopened. #2195 stops new ones (``filter_owned_document_ids``: the document
must belong to the *citing user's* org and not be soft-deleted; a caller with
no org keeps nothing); this one-off cleans up old rows.

Examples (from ``backend/``):
  python -m scripts.neutralize_cross_org_message_citations                 # dry run, audit to stdout
  python -m scripts.neutralize_cross_org_message_citations --audit-file a.ndjson
  python -m scripts.neutralize_cross_org_message_citations --apply --audit-file a.ndjson

Which orgs a citation's document may belong to:

* User-authored message (``user_id`` set): exactly the author's org, the same
  rule #2195 applies at write time. A document of the thread's own org that
  the (cross-org member) author does not belong to is unlinked. An author with
  no org (or a missing user row) keeps NONE of their document citations,
  matching ``filter_owned_document_ids`` for an org-less caller.
* Assistant/system message (``user_id`` is NULL): the citing user is unknown,
  so exact parity is impossible. The document may belong to any org that
  legitimately takes part in the thread: the workspace's org, the workspace
  owner's org, the thread creator's org, or the org of any active (not
  soft-deleted) workspace member. Workspace membership crosses orgs by
  design, so a cross-org member's own document is kept.
  RESIDUAL LEAK: an assistant citation to a document of one participating org
  that the requesting user could not see is kept, because the stored row
  does not say who asked. Removed members' orgs do not count. If none of
  those orgs is known, the row is counted as ``undetermined_org`` and left
  alone.

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

Audit trail (NDJSON; ``--apply`` REQUIRES ``--audit-file``, which is appended
to; a dry run may use stdout). Every line carries ``run_id`` and ``timestamp``:

* ``run_start``: mode, redacted target database, batch size.
* ``citation``, one per affected row, in BOTH modes: batch number, citation
  and message id, old document id, old document type when cleared,
  ``document_title_cleared`` (a boolean, never the text), fields cleared,
  document/thread/author org, reason, ``staged`` (the guarded UPDATE matched)
  and ``applied: false``. No titles or snippets. A cleared title equals the
  document's own title, so ``old_document_id`` is enough to restore it.
* ``batch_committed`` (``--apply`` only): the ids of that batch's citations
  that were actually written. A citation is applied iff its id appears in a
  ``batch_committed`` line of the same run.
* ``summary``: the counts.

Write-ahead: with ``--apply``, a batch's ``citation`` records are written,
flushed and fsynced BEFORE the batch commits. If that fails, the batch is
rolled back and the script exits 1, so nothing is ever unlinked without a
durable record. The ``batch_committed`` marker is written after the commit.

Counts: dry runs report ``would_unlink`` / ``would_clear_fields``; ``unlinked``
/ ``copied_fields_cleared`` count rows actually committed. The redacted target
database is also printed to stderr first.

Idempotent: unlinked rows have no ``document_id`` and drop out of the next
scan, so a rerun reports ``would_unlink: 0``. ``--apply`` commits per batch.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from typing import IO, Any, Dict, List, Optional, Sequence, Set, Tuple, cast
from uuid import UUID, uuid4

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

_Owner = aliased(User, name="owner")
_Creator = aliased(User, name="creator")
_Author = aliased(User, name="author")


class AuditWriteError(RuntimeError):
    """The audit trail could not be made durable; the batch was rolled back."""


class AuditLog:
    """NDJSON audit sink. ``sync`` makes everything written so far durable."""

    def __init__(self, stream: IO[str], *, durable: bool) -> None:
        self._stream = stream
        self._durable = durable

    def write(self, record: Dict[str, Any]) -> None:
        self._stream.write(json.dumps(record) + "\n")

    def sync(self) -> None:
        self._stream.flush()
        if self._durable:
            os.fsync(self._stream.fileno())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _type_value(document_type: Any) -> Optional[str]:
    value = getattr(document_type, "value", document_type)
    return None if value is None else str(value)


def _str(value: Optional[UUID]) -> Optional[str]:
    return None if value is None else str(value)


def _batch_query(after: Optional[UUID], batch_size: int) -> Any:
    stmt = (
        select(
            Citation.id,
            Citation.message_id,
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


def _allowed_orgs(row: Any, member_orgs: Dict[UUID, Set[UUID]]) -> Optional[Set[UUID]]:
    """Orgs the cited document may belong to; None when undeterminable."""
    if row.author_id is not None:
        # Exact #2195 parity; an org-less author keeps nothing (empty set).
        return set() if row.author_org is None else {row.author_org}
    candidates = {row.workspace_org, row.owner_org, row.creator_org}
    candidates |= member_orgs.get(row.workspace_id, set())
    allowed = {org for org in candidates if org is not None}
    return allowed or None


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
    audit: AuditLog,
    batch_size: int = DEFAULT_BATCH_SIZE,
    target: Optional[str] = None,
    run_id: Optional[str] = None,
) -> Dict[str, int]:
    """Scan message citations and (with ``apply``) unlink the disallowed ones.

    Raises ``AuditWriteError`` (after rolling the batch back) if a batch's
    audit records cannot be made durable before its commit.
    """
    run_id = run_id or uuid4().hex
    mode = "apply" if apply else "dry-run"
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
    try:
        audit.write(
            {
                "event": "run_start",
                "run_id": run_id,
                "timestamp": _now(),
                "mode": mode,
                "target": target,
                "batch_size": batch_size,
            }
        )
        audit.sync()
    except OSError as exc:
        raise AuditWriteError("could not write the audit header") from exc

    after: Optional[UUID] = None
    batch = 0
    while True:
        async with factory() as db:
            rows = (await db.execute(_batch_query(after, batch_size))).all()
            if not rows:
                break
            batch += 1
            after = rows[-1].id
            member_orgs = await _member_orgs(db, {row.workspace_id for row in rows})
            records: List[Dict[str, Any]] = []
            staged_ids: List[str] = []
            staged_cleared = 0
            for row in rows:
                counts["scanned"] += 1
                allowed = _allowed_orgs(row, member_orgs)
                if allowed is None:
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
                staged = False
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
                    staged = bool(result.rowcount)
                    if staged:
                        staged_ids.append(str(row.id))
                        if cleared:
                            staged_cleared += 1
                records.append(
                    {
                        "event": "citation",
                        "run_id": run_id,
                        "timestamp": _now(),
                        "mode": mode,
                        "batch": batch,
                        "applied": False,
                        "staged": staged,
                        "reason": reason,
                        "citation_id": str(row.id),
                        "message_id": str(row.message_id),
                        "old_document_id": str(row.document_id),
                        "old_document_type": (
                            row.document_type if "document_type" in cleared else None
                        ),
                        "document_title_cleared": "document_title" in cleared,
                        "fields_cleared": cleared,
                        "doc_org": _str(row.doc_org),
                        "thread_org": _str(row.workspace_org or row.owner_org),
                        "author_org": _str(row.author_org),
                    }
                )
            # Write-ahead: the records must be durable before anything commits.
            try:
                for record in records:
                    audit.write(record)
                audit.sync()
            except OSError as exc:
                await db.rollback()
                raise AuditWriteError(
                    f"audit write failed in batch {batch}; batch rolled back"
                ) from exc
            if not apply:
                continue
            await db.commit()
            counts["unlinked"] += len(staged_ids)
            counts["copied_fields_cleared"] += staged_cleared
        if records:
            try:
                audit.write(
                    {
                        "event": "batch_committed",
                        "run_id": run_id,
                        "timestamp": _now(),
                        "batch": batch,
                        "applied_citation_ids": staged_ids,
                    }
                )
                audit.sync()
            except OSError as exc:
                raise AuditWriteError(
                    f"batch {batch} COMMITTED but its batch_committed marker was "
                    "not written; its citation records with staged=true are the "
                    "rows that were unlinked"
                ) from exc
        logger.info("neutralize_cross_org_message_citations progress: %s", counts)
    return counts


def _summary(run_id: str, mode: str, counts: Dict[str, int]) -> Dict[str, Any]:
    return {
        "event": "summary",
        "run_id": run_id,
        "timestamp": _now(),
        "mode": mode,
        **counts,
    }


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
        help="append the NDJSON audit trail here (required with --apply; "
        "default for a dry run: stdout)",
    )
    args = parser.parse_args(argv)
    if args.batch_size < 1:
        parser.error("--batch-size must be >= 1")
    if args.apply and not args.audit_file:
        parser.error("--apply requires --audit-file")
    return args


def main(argv: Optional[Sequence[str]] = None) -> None:
    args = _parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    mode = "apply" if args.apply else "dry-run"
    run_id = uuid4().hex

    async def run() -> Dict[str, int]:
        factory, url = _open_target()
        target = redact_url(url)
        print(f"target database ({mode}): {target}", file=sys.stderr)
        if args.audit_file is None:
            return await neutralize(
                factory,
                apply=args.apply,
                batch_size=args.batch_size,
                target=target,
                run_id=run_id,
                audit=AuditLog(sys.stdout, durable=False),
            )
        with open(args.audit_file, "a", encoding="utf-8") as sink:
            audit = AuditLog(sink, durable=True)
            counts = await neutralize(
                factory,
                apply=args.apply,
                batch_size=args.batch_size,
                target=target,
                run_id=run_id,
                audit=audit,
            )
            audit.write(_summary(run_id, mode, counts))
            audit.sync()
            return counts

    try:
        result = asyncio.run(run())
    except AuditWriteError as exc:
        print(f"ERROR: {exc}: {exc.__cause__!r}", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(_summary(run_id, mode, result)))


if __name__ == "__main__":
    main()
