# KG batch dispatcher fanout mutation checks

Dated record, 2026-11-24, for GOO-716 (base `origin/develop` `f2ea8031c`).
Procedure: [Mutation verification](../engineering/testing.md#mutation-verification-race--idempotency-tests).

The KG batch dispatcher (`kg_extract_entities_job` with no `document_index`)
writes the private `_kg_fanout_published` key into `ProcessingJob.result` after
its per-document group is published. A failed marker write is rolled back and
logged; the published batch stays RUNNING. A RUNNING batch skips as a
duplicate when the message has another task id than the one that started the
job, or when it carries the marker or a `_kg_document_results` checkpoint. The
starting message's own redelivery without either fails the job with error type
`DispatcherWorkerLost`, which needs an explicit retry.

Each check disabled one guard in `backend/src/tasks/processing_tasks.py`, ran
the focused test, restored the source from a byte-for-byte pre-mutation copy
(`cmp` clean), and reran the same test green. Line numbers are as of this
change. Commands run from `backend/`:

```sh
python -P -m pytest -q --tb=short tests/unit/tasks/test_kg_extract_fanout.py::<test>
```

| Guard (line) | Mutation | Test | Observed failure |
| --- | --- | --- | --- |
| Evidence checks in the RUNNING batch guard (1287) | condition `True` (RUNNING and more than one document alone skips) | `test_dispatcher_lost_before_fanout_fails_job` | `{'status': 'running'} != {'status': 'failed'}` |
| Marker check (1288) | `False` (marker never trusted) | `test_redelivered_batch_dispatcher_does_not_publish_twice` | `{'status': 'failed'} != {'status': 'running'}` |
| Task-id check (1287) | `False` (other messages treated as own redelivery) | `test_other_dispatcher_message_skips_while_owner_publishes` | `{'status': 'failed'} != {'status': 'running'}` |
| Child-checkpoint check (1289) | `False` | `test_redelivered_dispatcher_with_child_checkpoint_skips` | `{'status': 'failed'} != {'status': 'running'}` |
| Marker write after `apply_async()` (1341) | write removed | `test_batch_dispatch_records_fanout_after_publish` | `Right contains 1 more item: {'_kg_fanout_published': True}` |
| Marker write merges the locked `result` (1340) | `fanout_state = {}` | `test_batch_dispatch_records_fanout_after_publish` | `Right contains 1 more item: {'_kg_document_results': {'0': {'entities_created': 1}}}` |
| Marker write failure is contained (1344-1348) | `raise` added after the log | `test_failed_fanout_record_keeps_published_batch_running` | `assert False` where `False = successful()` |
| RETRYING clears `result` (1305) | `job.result = None` removed | `test_retry_clears_saved_document_results` | `At index 0 diff: {'_kg_document_results': ..., '_kg_fanout_published': True} != None` |
