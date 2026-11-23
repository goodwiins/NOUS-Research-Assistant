// GOO-718: the arxiv-ingest-paper task used to poll
// /api/v1/processing/jobs/{job_id} with a *document* id taken from a
// `document_ids` field that POST /api/v1/arxiv/ingest never returns (the route
// queues a FastAPI background task and answers {message, paper_count,
// status}). The poll was unreachable, and wrong if it ever ran. The task must
// report the ingest as accepted and make no further backend call, whatever
// the response carries.
// Run: node --experimental-transform-types --experimental-test-module-mocks \
//        --test tests/trigger/*.test.mjs
import assert from "node:assert/strict";
import { registerHooks } from "node:module";
import { afterEach, mock, test } from "node:test";

const quiet = { info() {}, warn() {}, error() {}, log() {} };
mock.module("@trigger.dev/sdk", {
  namedExports: {
    logger: quiet,
    task: (definition) => definition,
    wait: { for: async () => {} },
  },
});

// Task files import siblings without an extension ("../_lib/backend-client"),
// which the Trigger.dev bundler resolves and Node does not. Resolve those to
// the .ts file so the real backend client runs against a stubbed fetch.
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

const { ingestPaper } = await import("../../src/trigger/arxiv/ingest-paper.ts");

const realFetch = globalThis.fetch;
let calls = [];
afterEach(() => {
  globalThis.fetch = realFetch;
});

function stubFetch(body) {
  calls = [];
  globalThis.fetch = async (url, init) => {
    calls.push(`${init?.method} ${new URL(url).pathname}`);
    return new Response(JSON.stringify(body), { status: 200 });
  };
}

const payload = { paperId: "2301.07041", downloadPdf: true, userId: "u" };

test("the real /arxiv/ingest response is reported as accepted", async () => {
  stubFetch({
    message: "ArXiv paper ingestion started",
    paper_count: 1,
    status: "processing",
  });
  const out = await ingestPaper.run(payload);
  assert.deepEqual(out, { status: "accepted", paperId: "2301.07041" });
  assert.deepEqual(calls, ["POST /api/v1/arxiv/ingest"]);
});

test("a document id is never polled as a processing job id", async () => {
  stubFetch({ status: "processing", document_ids: ["doc-1"] });
  const out = await ingestPaper.run(payload);
  assert.deepEqual(out, { status: "accepted", paperId: "2301.07041" });
  assert.deepEqual(calls, ["POST /api/v1/arxiv/ingest"]);
});

// GOO-755: a failed ingest POST must reject the task. The task retry is the
// only retry layer for this write (the client sends a POST once), so a task
// that swallowed the error would report "accepted" for a paper the backend
// never queued, and would never retry it.
function stubStatus(status) {
  calls = [];
  globalThis.fetch = async (url, init) => {
    calls.push(`${init?.method} ${new URL(url).pathname}`);
    return new Response(JSON.stringify({ detail: "x" }), { status });
  };
}

test("a 500 from the ingest POST rejects the task, sent once per attempt", async () => {
  stubStatus(500);
  await assert.rejects(ingestPaper.run(payload), (err) => err.status === 500);
  assert.deepEqual(calls, ["POST /api/v1/arxiv/ingest"]);
});

test("a 4xx from the ingest POST rejects the task too", async () => {
  stubStatus(422);
  await assert.rejects(ingestPaper.run(payload), (err) => err.status === 422);
});
