"""Guard: KG job endpoints reserve the job durably, then publish (GOO-464).

`create_merge_job` (POST /merge-jobs) and `create_extraction_job`
(POST /extraction-jobs) once committed a PENDING job and then called
`apply_async` with no error handling, orphaning the row when the broker was
down. The follow-up fix flushed, published and only then committed, which
closed the orphan but let a fast worker receive the message before the job row
was durable (GOO-464).

Both now go through `_reserve_and_publish_job`: generate the task id, stamp it
on the row as a QUEUED attempt and commit, then publish under that id. A
publish failure fails the reserved attempt (no orphan) and returns 503.
Behaviour is covered in `tests/unit/api/test_knowledge_graph_job_publish_order.py`;
this guard pins the shape so neither endpoint regresses to publish-first.
"""

import re
from pathlib import Path

# tests/ -> backend/
ROUTER = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "api"
    / "search"
    / "knowledge_graph.py"
)

ENDPOINTS = [
    ("create_merge_job", "kg_merge_entities_job"),
    ("create_extraction_job", "kg_extract_entities_job"),
]


def _fn_source(name: str) -> str:
    source = ROUTER.read_text()
    start = source.index(f"def {name}(")
    m = re.search(r"\n(async def |def |@)", source[start + 10 :])
    end = start + 10 + m.start() if m else len(source)
    return source[start:end]


def test_endpoints_delegate_to_reserve_then_publish():
    for name, task in ENDPOINTS:
        fn = _fn_source(name)
        assert f"_reserve_and_publish_job(db, job, {task}," in fn, (
            f"{name} must enqueue through _reserve_and_publish_job so the job "
            "row is committed before its message can reach a worker"
        )
        assert f"{task}.apply_async(" not in fn, (
            f"{name} must not publish {task} directly; a publish before the "
            "commit lets a worker read a job row that does not exist yet"
        )


def test_helper_reserves_before_publishing_with_the_reserved_id():
    fn = _fn_source("_reserve_and_publish_job")
    publish = fn.index(".apply_async(")
    assert (
        "reserve_job(db, job," in fn[:publish]
    ), "the reservation must be committed (reserve_job) before publish"
    assert (
        "task_id=task_id" in fn[publish:]
    ), "the message must carry the task id persisted on the reserved row"
    assert (
        "fail_unpublished_job(db, job, task_id)" in fn[publish:]
    ), "a publish failure must fail the reserved attempt, not orphan it"


def test_router_helper_owns_no_transaction():
    """Persistence lives in services/processing/job_reservation.py (one commit
    per function); the router helper only publishes and maps errors to HTTP."""
    fn = _fn_source("_reserve_and_publish_job")
    for call in ("db.commit(", "db.flush(", "db.refresh(", "db.add("):
        assert call not in fn, f"_reserve_and_publish_job must not call {call}...)"
