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
