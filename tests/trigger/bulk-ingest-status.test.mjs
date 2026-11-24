// GOO-764: arxiv-bulk-ingest used to return `completed` whatever its children
// did, so a batch where every paper failed looked like a success. It must
// report `failed` when every child fails, `partial` (with counts) when some
// fail, and `completed` only when none fail.
// Run: node --experimental-transform-types --experimental-test-module-mocks \
//        --test tests/trigger/*.test.mjs
import assert from "node:assert/strict";
import { registerHooks } from "node:module";
import { afterEach, mock, test } from "node:test";

const quiet = { info() {}, warn() {}, error() {}, log() {} };
const meta = new Map();
mock.module("@trigger.dev/sdk", {
  namedExports: {
    logger: quiet,
    task: (definition) => definition,
    metadata: { set: (k, v) => meta.set(k, v) },
  },
});

// Task files import siblings without an extension; resolve them to .ts.
registerHooks({
  resolve(specifier, context, next) {
    if (
      specifier.startsWith(".") &&
      !/\.[cm]?[jt]s$/.test(specifier) &&
      context.parentURL?.includes("/src/trigger/")
    ) {
      return next(`${specifier}.ts`, context);
    }
    return next(specifier, context);
  },
});

const { bulkIngestArxiv } = await import("../../src/trigger/arxiv/bulk-ingest.ts");
const { ingestPaper } = await import("../../src/trigger/arxiv/ingest-paper.ts");
const { bulkIngestStatus } = await import("../../src/trigger/arxiv/tally.ts");

const realFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = realFetch;
  delete ingestPaper.batchTriggerAndWait;
  meta.clear();
});

const papers = [
  { id: "2401.00001", title: "a" },
  { id: "2401.00002", title: "b" },
  { id: "2401.00003", title: "c" },
];

/** Search answers `papers`; child run i succeeds unless failIdx has i. */
function arrange(failIdx) {
  globalThis.fetch = async () => new Response(JSON.stringify(papers), { status: 200 });
  let i = 0;
  ingestPaper.batchTriggerAndWait = async (items) => ({
    runs: items.map(({ payload }) =>
      failIdx.has(i++)
        ? { ok: false, error: new Error("ingest failed") }
        : { ok: true, output: { status: "accepted", paperId: payload.paperId } },
    ),
  });
}

const payload = { query: "q", maxResults: 3, batchSize: 2, userId: "u" };

test("every child failing reports failed, not completed", async () => {
  arrange(new Set([0, 1, 2]));
  const out = await bulkIngestArxiv.run(payload);
  assert.deepEqual(out, { status: "failed", accepted: 0, failed: 3, total: 3 });
  assert.equal(meta.get("status"), "failed");
});

test("some children failing reports partial with counts", async () => {
  arrange(new Set([1]));
  const out = await bulkIngestArxiv.run(payload);
  assert.deepEqual(out, { status: "partial", accepted: 2, failed: 1, total: 3 });
  assert.equal(meta.get("status"), "partial");
});

test("no child failing reports completed", async () => {
  arrange(new Set());
  const out = await bulkIngestArxiv.run(payload);
  assert.deepEqual(out, { status: "completed", accepted: 3, failed: 0, total: 3 });
  assert.equal(meta.get("status"), "completed");
});

test("an empty search is completed with nothing to ingest", async () => {
  globalThis.fetch = async () => new Response("[]", { status: 200 });
  assert.deepEqual(await bulkIngestArxiv.run(payload), {
    status: "completed",
    accepted: 0,
    failed: 0,
    total: 0,
  });
});

test("bulkIngestStatus never says completed when anything failed", () => {
  assert.equal(bulkIngestStatus({ accepted: 0, failed: 0 }), "completed");
  assert.equal(bulkIngestStatus({ accepted: 5, failed: 0 }), "completed");
  assert.equal(bulkIngestStatus({ accepted: 5, failed: 1 }), "partial");
  assert.equal(bulkIngestStatus({ accepted: 0, failed: 1 }), "failed");
});
