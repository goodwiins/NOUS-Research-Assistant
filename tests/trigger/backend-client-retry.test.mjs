// GOO-656: the shared backend client must not retry ambiguous writes.
// Run: node --experimental-transform-types --experimental-test-module-mocks \
//        --test tests/trigger/*.test.mjs
// (backend-client.ts uses parameter properties, which need type transforms;
// the SDK is mocked so the test never loads the Trigger.dev runtime.)
import assert from "node:assert/strict";
import { afterEach, mock, test } from "node:test";

const quiet = { info() {}, warn() {}, error() {}, log() {} };
mock.module("@trigger.dev/sdk", { namedExports: { logger: quiet } });

const { BackendApiError, backendClient } = await import(
  "../../src/trigger/_lib/backend-client.ts"
);

const realFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = realFetch;
});

function stubFetch(status) {
  const calls = [];
  globalThis.fetch = async (url, init) => {
    calls.push({ url, method: init?.method });
    return new Response(JSON.stringify({ detail: "x" }), { status });
  };
  return calls;
}

test("a POST that answers 5xx is sent exactly once", async () => {
  const calls = stubFetch(503);
  await assert.rejects(
    backendClient.post("/api/v1/agent/execute", { a: 1 }),
    (err) => err instanceof BackendApiError && err.status === 503,
  );
  assert.equal(calls.length, 1);
});

test("a POST whose transport fails is sent exactly once", async () => {
  let n = 0;
  globalThis.fetch = async () => {
    n++;
    throw new TypeError("fetch failed");
  };
  await assert.rejects(backendClient.post("/api/v1/arxiv/ingest", {}));
  assert.equal(n, 1);
});

test("an opted-in idempotent POST is retried", { timeout: 10_000 }, async () => {
  const calls = stubFetch(503);
  await assert.rejects(
    backendClient.post("/api/v1/arxiv/search", {}, undefined, undefined, {
      idempotent: true,
    }),
  );
  assert.equal(calls.length, 3);
});

// GOO-678: a response that fails its schema is a contract mismatch, not a
// transient fault. Retrying it only delays the same ZodError.
test("a response that fails its schema is fetched once, even when idempotent", async () => {
  const { z } = await import("zod");
  const calls = stubFetch(200);
  await assert.rejects(
    backendClient.get("/api/v1/workers/status", z.object({ active_workers: z.number() })),
    (err) => err instanceof z.ZodError,
  );
  assert.equal(calls.length, 1);
});
