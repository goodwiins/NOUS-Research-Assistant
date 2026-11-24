# KG batch dispatcher fanout mutation checks

Dated record, 2026-11-24, for GOO-716 (base `origin/develop` `f2ea8031c`).
Procedure: [Mutation verification](../engineering/testing.md#mutation-verification-race--idempotency-tests).

The KG batch dispatcher (`kg_extract_entities_job` with no `document_index`)
writes the private `_kg_fanout_published` key into `ProcessingJob.result` after
its per-document group is published. A redelivered RUNNING batch skips only
with that marker; without it the job fails with error type
`DispatcherWorkerLost` and needs an explicit retry.

Each check disabled one guard in `backend/src/tasks/processing_tasks.py`, ran
the focused test, restored the source from a byte-for-byte pre-mutation copy
(`cmp` clean), and reran the same test green. Line numbers are as of this
change. Commands run from `backend/`:

```sh
python -P -m pytest -q --tb=short tests/unit/tasks/test_kg_extract_fanout.py::<test>
```

| Guard (line) | Mutation | Test | Observed failure |
| --- | --- | --- | --- |
| Fanout-marker check in the RUNNING batch guard (1282) | `if True:` (RUNNING and more than one document alone skips) | `test_dispatcher_lost_before_fanout_fails_job` | `{'status': 'running'} != {'status': 'failed'}` |
| Same check (1282) | `if False:` (marker never trusted) | `test_redelivered_batch_dispatcher_does_not_publish_twice` | `{'status': 'failed'} != {'status': 'running'}` |
| Marker write after `apply_async()` (1330) | write removed | `test_batch_dispatch_records_fanout_after_publish` | `Right contains 1 more item: {'_kg_fanout_published': True}` |
| Marker write merges the locked `result` (1329) | `fanout_state = {}` | `test_batch_dispatch_records_fanout_after_publish` | `Right contains 1 more item: {'_kg_document_results': {'0': {'entities_created': 1}}}` |
| RETRYING clears `result` (1297) | `job.result = None` removed | `test_retry_clears_saved_document_results` | `At index 0 diff: {'_kg_document_results': ..., '_kg_fanout_published': True} != None` |
