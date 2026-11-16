"""GOO-463: every producer the lost-job reconciler covers records its task id.

``reconcile_lost_processing_jobs`` re-enqueues PENDING rows whose
``celery_task_id`` is NULL, reading NULL as "the dispatch never reached the
broker". A producer that publishes but never stores the id makes a job that is
merely queued behind a backlog look lost, so the reconciler publishes a
duplicate. Each producer of PENDING DOCUMENT_INGESTION / ENTITY_EXTRACTION rows
must therefore persist the published task id through
``record_dispatched_task_id`` once the dispatch has landed.
"""

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SRC = Path(__file__).resolve().parents[3] / "src"

# (module, enclosing function) for each producer of reconciler-eligible rows.
PRODUCERS = [
    ("api/documents/documents.py", "async def reprocess_document("),
    ("api/documents/files.py", "async def reprocess_file("),
    ("services/documents/file_service.py", "    async def upload_file("),
    ("api/research/projects.py", "async def add_document_to_project("),
]


def _fn_source(module: str, signature: str) -> str:
    source = (SRC / module).read_text()
    start = source.index(signature)
    indent = signature[: len(signature) - len(signature.lstrip())]
    rest = source[start + len(signature) :]
    m = re.search(rf"\n{indent}(async def |def |@)", rest)
    end = start + len(signature) + m.start() if m else len(source)
    return source[start:end]


@pytest.mark.parametrize("module,signature", PRODUCERS)
def test_producer_records_dispatched_task_id(module: str, signature: str) -> None:
    fn = _fn_source(module, signature)
    assert "record_dispatched_task_id(" in fn, (
        f"{module}:{signature.strip()} publishes a reconciler-eligible job "
        "without persisting the published task id; a backlogged message then "
        "reads as never-dispatched and the lost-job reconciler duplicates it"
    )
