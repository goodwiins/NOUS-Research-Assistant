// Offline checks for the pure helpers under src/trigger/ (GOO-656).
// Run: node --test tests/trigger/   (Node >= 22.18 strips TypeScript types)
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  deterministicUuid,
  withClientMessageId,
} from "../../src/trigger/_lib/idempotency.ts";
import { emptyTally, tallyIngestRuns } from "../../src/trigger/arxiv/tally.ts";

const UUID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-5[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

test("deterministicUuid is stable per seed and UUID-shaped", () => {
  const a = deterministicUuid("trigger:execute-agent:run_123");
  assert.match(a, UUID_RE);
  assert.equal(a, deterministicUuid("trigger:execute-agent:run_123"));
  assert.notEqual(a, deterministicUuid("trigger:execute-agent:run_124"));
});

test("withClientMessageId stamps only the newest user turn", () => {
  const messages = [
    { role: "user", content: "first" },
    { role: "assistant", content: "reply" },
    { role: "user", content: "second" },
    { role: "assistant", content: "trailing" },
  ];
  const out = withClientMessageId(messages, "key-1");
  assert.equal(out[2].client_message_id, "key-1");
  assert.equal(out[0].client_message_id, undefined);
  assert.equal(out[1].client_message_id, undefined);
  assert.equal(out[3].client_message_id, undefined);
  // input is not mutated
  assert.equal(messages[2].client_message_id, undefined);
});

test("withClientMessageId keeps a caller-supplied key", () => {
  const out = withClientMessageId(
    [{ role: "user", content: "hi", client_message_id: "caller" }],
    "derived",
  );
  assert.equal(out[0].client_message_id, "caller");
});

test("tallyIngestRuns counts only completed children as ingested", () => {
  const tally = tallyIngestRuns([
    { ok: true, output: { status: "completed" } },
    { ok: true, output: { status: "failed" } },
    { ok: true, output: { status: "timeout" } },
    { ok: true, output: { status: "skipped" } },
    { ok: true, output: { status: "accepted" } },
    { ok: true, output: undefined },
    { ok: false, error: new Error("boom") },
  ]);
  assert.deepEqual(tally, { ingested: 1, accepted: 1, skipped: 1, failed: 4 });
});

test("tallyIngestRuns accumulates across batches", () => {
  const tally = emptyTally();
  tallyIngestRuns([{ ok: true, output: { status: "completed" } }], tally);
  tallyIngestRuns([{ ok: false }], tally);
  assert.deepEqual(tally, { ingested: 1, accepted: 0, skipped: 0, failed: 1 });
});
