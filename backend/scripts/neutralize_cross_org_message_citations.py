"""CLI: unlink message citations that point at another org's document (GOO-671).

Before #2195 (GOO-631), ``POST /api/v2/messages`` stored ``citations[].document_id``
from the request without an ownership check. The message presenter falls back
to ``citation.document.title`` / ``document_type``, so a citation that names a
document from a different organization still shows that document whenever its
thread is reopened. #2195 stops new ones; this one-off cleans up old rows.

Examples (from ``backend/``):
  python -m scripts.neutralize_cross_org_message_citations           # dry run (default)
  python -m scripts.neutralize_cross_org_message_citations --apply

What it does, per citation attached to a chat message with a ``document_id``:

* The thread's organization is ``workspaces.organization_id``, or the workspace
  owner's organization when that is NULL (legacy rows; same rule as
  ``project_access.py``). If neither is set, the row is counted as
  ``undetermined_org`` and left alone.
* If the cited document's organization differs from the thread's (or the
  document is gone), the citation is *cross-org*. ``--apply`` sets its
  ``document_id`` to NULL and also clears ``document_title`` / ``document_type``
  when they equal the foreign document's own values (only that document could
  have supplied them). The client's own snippet, label and scores stay.

Same-org citations, citations without a document, and citations not attached
to a message are never touched. A dry run classifies exactly as ``--apply``
would and writes nothing. Idempotent: unlinked rows have no ``document_id`` and
drop out of the next scan, so a rerun reports ``cross_org: 0``. ``--apply``
commits per batch. Output is counts only, never ids or titles.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from typing import Any, Dict, Optional, cast
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.models.chat_message import ChatMessage
from src.models.citation import Citation
from src.models.conversation import Conversation
from src.models.document import Document
from src.models.thread import Thread
from src.models.user import User
from src.models.workspace import Workspace

logger = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE = 500


def _type_value(document_type: Any) -> Optional[str]:
    value = getattr(document_type, "value", document_type)
    return None if value is None else str(value)


def _batch_query(after: Optional[UUID], batch_size: int) -> Any:
    thread_org = func.coalesce(
        Workspace.organization_id,
        User.organization_id,
        type_=Workspace.organization_id.type,
    ).label("thread_org")
    stmt = (
        select(
            Citation.id,
            Citation.document_id,
            Citation.document_title,
            Citation.document_type,
            Document.id.label("doc_exists"),
            Document.organization_id.label("doc_org"),
            Document.title.label("doc_title"),
            Document.document_type.label("doc_type"),
            thread_org,
        )
        .join(ChatMessage, ChatMessage.id == Citation.message_id)
        .join(Thread, Thread.id == ChatMessage.thread_id)
        .join(Conversation, Conversation.id == Thread.conversation_id)
        .join(Workspace, Workspace.id == Conversation.workspace_id)
        .outerjoin(User, User.id == Workspace.owner_id)
        .outerjoin(Document, Document.id == Citation.document_id)
        .where(Citation.document_id.is_not(None))
        .order_by(Citation.id)
        .limit(batch_size)
    )
    if after is not None:
        stmt = stmt.where(Citation.id > after)
    return stmt


async def neutralize(
    factory: async_sessionmaker[AsyncSession],
    *,
    apply: bool,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> Dict[str, int]:
    """Scan message citations and (with ``apply``) unlink cross-org ones."""
    counts = {
        "scanned": 0,
        "cross_org": 0,
        "unlinked": 0,
        "copied_fields_cleared": 0,
        "undetermined_org": 0,
    }
    after: Optional[UUID] = None
    while True:
        async with factory() as db:
            rows = (await db.execute(_batch_query(after, batch_size))).all()
            if not rows:
                break
            after = rows[-1].id
            for row in rows:
                counts["scanned"] += 1
                if row.thread_org is None:
                    counts["undetermined_org"] += 1
                    continue
                same_org = row.doc_exists is not None and row.doc_org == row.thread_org
                if same_org:
                    continue
                counts["cross_org"] += 1
                values: Dict[str, Any] = {"document_id": None}
                if row.doc_exists is not None:
                    if (
                        row.document_title is not None
                        and row.document_title == row.doc_title
                    ):
                        values["document_title"] = None
                    if row.document_type is not None and row.document_type == (
                        _type_value(row.doc_type)
                    ):
                        values["document_type"] = None
                if not apply:
                    continue
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
                if result.rowcount:
                    counts["unlinked"] += 1
                    if len(values) > 1:
                        counts["copied_fields_cleared"] += 1
            if apply:
                await db.commit()
        logger.info("neutralize_cross_org_message_citations progress: %s", counts)
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
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
    args = parser.parse_args()
    if args.batch_size < 1:
        parser.error("--batch-size must be >= 1")

    logging.basicConfig(level=logging.INFO)
    from src.core.database import AsyncSessionLocal

    result = asyncio.run(
        neutralize(AsyncSessionLocal, apply=args.apply, batch_size=args.batch_size)
    )
    print(json.dumps({"mode": "apply" if args.apply else "dry-run", **result}))


if __name__ == "__main__":
    main()
