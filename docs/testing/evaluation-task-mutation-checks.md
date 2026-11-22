# Evaluation task mutation checks (GOO-684)

Recorded 2026-11-22 for the evaluation Celery tasks
(`backend/src/tasks/evaluation_tasks.py`) after the review of #2224
(GOO-675). It follows the procedure in
[`docs/engineering/testing.md`](../engineering/testing.md#mutation-verification-race--idempotency-tests).
The checks are unit-level: the job session is a `MagicMock`, and Celery runs
the tasks eagerly with `.apply()`. The Postgres `SELECT … FOR UPDATE` claim
and a real broker redelivery are NOT RUN.

Procedure for each mutant:
1. Apply it as an exact-string replacement that asserts a single match.
2. Run the focused command.
3. Restore the file from a private copy and confirm `git diff` shows only the
   intended change.
4. Rerun the command and confirm it is green.

```sh
# from backend/
.venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/unit/tasks/test_evaluation_failure_paths.py \
  tests/unit/tasks/test_evaluation_idempotency.py \
  tests/unit/tasks/test_evaluation_worker_lost.py \
  tests/unit/tasks/test_evaluation_time_limits.py
```

Line numbers are for the GOO-684 branch.

| Guard (line) | Mutation | Observed mutant failure |
|---|---|---|
| `on_failure` keeps a terminal job (`evaluation_tasks.py:208`) | `if job:` | `test_on_failure_leaves_a_terminal_job_alone[completed\|failed\|cancelled]`: `fail_job` is called and the task's own message is replaced with the late error text. |
| Real-time job started on creation (`evaluation_tasks.py:631`) | `job.start_job()` removed | `test_real_time_job_is_running_while_it_evaluates`: the job is still `None`/PENDING while it is evaluated, so a later redelivery could never tell that the attempt died. |
| Real-time redelivery fails any unfinished job (`evaluation_tasks.py:592`) | `== EvaluationStatus.RUNNING.value` (the #2224 shape) | `test_real_time_redelivery_fails_the_unfinished_job[pending]`: a PENDING row from a dead attempt is skipped and stays PENDING forever. |
| Batch commits each query's metric (`evaluation_tasks.py:503`) | the per-query `db.commit()` removed | `test_batch_soft_limit_keeps_the_metrics_already_evaluated`: q1's metric is not committed before the soft limit's rollback, so the "metrics already saved" message would be false. |
| Batch metric keeps its own query text (`evaluation_tasks.py:485`) | `query=queries[len(metrics_results) - 1]` (index pairing) | `test_batch_metric_keeps_its_own_query_when_an_earlier_one_fails`: q2's metric is saved as q1. |
| Search pipeline re-raises the soft limit (`rag_evaluation_service.py:677`) | `except ZeroDivisionError:` | `test_search_pipeline_does_not_swallow_the_soft_time_limit`: `DID NOT RAISE SoftTimeLimitExceeded`, so the pipeline keeps evaluating until the hard kill. |
| A skip raises `Ignore` (`evaluation_tasks.py:177`) | `return response` | 13 tests, e.g. `test_terminal_redelivery_is_ignored_not_success`: state `SUCCESS`. Real-time and the job tasks also run past the guard. |
| Batch handler lets `Ignore` through (`evaluation_tasks.py:536`) | the `except Ignore: raise` clause removed | `test_batch_short_circuits_terminal_job`: `fail_job` is called on a COMPLETED job, because `Ignore` is an `Exception`. |
| Rag-triad handler lets `Ignore` through (`evaluation_tasks.py:403`) | the `except Ignore: raise` clause removed | `test_rag_triad_short_circuits_terminal_job[completed\|failed\|cancelled]`: `fail_job` is called on a terminal job. |
| Hard limit leaves visibility headroom (`config.py:893`) | `lt=3600` | `test_hard_limit_leaves_headroom_under_the_visibility_timeout`: `DID NOT RAISE ValidationError` for 3599s. |

All 10 mutants failed at least one test. Every restore left the source as
intended, and the four files were green again after the last restore.

## GOO-689 (review of #2231), 2026-11-22

Item 1 was first reproduced on a throwaway local PostgreSQL 14 cluster, built
from the full model schema (`Base.metadata.create_all`). It ran on a Unix
socket in a temporary directory and was deleted afterwards.
`evaluation_metrics.job_id` is NOT NULL there. `evaluate_search_pipeline`
returned 0 of 2 queries and wrote 0 rows, because each query's metric commit
raised `NotNullViolation` and the pipeline's per-query handler dropped it.

The real-Postgres tests live in
`tests/unit/tasks/test_evaluation_metrics_postgres.py` and are skipped unless
`ORCHESTRATION_TEST_DATABASE_URL` points at a disposable database. They ran
here against that throwaway cluster.

```sh
# from backend/
ORCHESTRATION_TEST_DATABASE_URL="${DISPOSABLE_PG_URL:?}" .venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/unit/tasks/test_evaluation_failure_paths.py \
  tests/unit/tasks/test_evaluation_metrics_postgres.py \
  tests/unit/tasks/test_evaluation_idempotency.py \
  tests/unit/tasks/test_evaluation_worker_lost.py
```

| Guard (line) | Mutation | Observed mutant failure |
|---|---|---|
| Pipeline passes its job to the triad (`rag_evaluation_service.py:686`) | `None` (the develop shape) | `test_pipeline_with_a_job_writes_its_metrics` (Postgres): `NotNullViolation`, 0 results instead of 2. Also `test_pipeline_passes_the_job_to_the_triad_evaluation`. |
| Triad writes nothing without a job (`rag_evaluation_service.py:229`) | `if False:` | `test_pipeline_without_a_job_returns_scores_and_writes_nothing` (Postgres): the scores are dropped. Also `test_triad_without_a_job_scores_but_writes_nothing`. |
| Batch passes its job to the pipeline (`evaluation_tasks.py:489`) | the `job_id=` argument removed | `test_batch_passes_its_job_to_the_pipeline` |
| `complete_job` clears the error (`models/evaluation.py:138`) | the line removed | `test_complete_job_clears_the_error_message`: the worker-lost text stays on a COMPLETED job. |
| Real-time takes the task-id lock first (`evaluation_tasks.py:616`) | the call removed | `test_real_time_locks_its_task_id_before_looking_for_a_job`. The lock itself is proven by `test_real_time_task_lock_blocks_a_second_delivery` (Postgres). |
| `processed_count` counts saved queries only (`evaluation_tasks.py:524`) | `update_progress(i + 1)` | `test_batch_metric_keeps_its_own_query_when_an_earlier_one_fails` |
| Metric commit inside its `try`, with rollback (`evaluation_tasks.py:532`) | `db.rollback()` removed | `test_batch_metric_commit_failure_is_rolled_back_and_the_run_continues` |

All 7 mutants failed at least one test, and every restore was green.
