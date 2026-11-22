import assert from "node:assert/strict";
import { test } from "node:test";
import { JsonRpcProcess, TransportLoss } from "../src/rpc.ts";

// TD-17: two overlapping writes (an RPC call plus a callback response, or two
// calls) used to throw TransportLoss("stdin backpressure") and tear down the
// whole Codex session. Writes are now serialized; only real stdin failures are
// transport loss.
// Command: pnpm --filter @nous/harness-bridge test test/rpc-writes.test.ts

/** A tiny JSON-RPC peer: answers every request with its method name. */
const ECHO = `
let buf = "";
process.stdin.on("data", (d) => {
  buf += d;
  let i;
  while ((i = buf.indexOf("\\n")) >= 0) {
    const m = JSON.parse(buf.slice(0, i));
    buf = buf.slice(i + 1);
    if (m.id !== undefined && m.method !== undefined)
      process.stdout.write(JSON.stringify({ id: m.id, result: { echo: m.method } }) + "\\n");
  }
});
`;

function echoProcess() {
  const failures: Error[] = [];
  const rpc = new JsonRpcProcess(
    process.execPath,
    ["-e", ECHO],
    async () => {},
    (e) => failures.push(e),
  );
  return { rpc, failures };
}

test("concurrent calls and notifications are serialized, not treated as transport loss", async () => {
  const { rpc, failures } = echoProcess();
  try {
    const results = await Promise.all([
      rpc.call("a", {}),
      rpc.call("b", {}),
      rpc.notify("note"),
      rpc.call("c", {}),
      rpc.respond(99, { ok: true }),
    ]);
    assert.deepEqual(results[0], { echo: "a" });
    assert.deepEqual(results[1], { echo: "b" });
    assert.deepEqual(results[3], { echo: "c" });
    assert.deepEqual(failures, []);
  } finally {
    await rpc.close();
  }
});

test("a large concurrent burst is written in order without failing the transport", async () => {
  const { rpc, failures } = echoProcess();
  try {
    const big = "x".repeat(200_000);
    const calls = Array.from({ length: 20 }, (_, i) =>
      rpc.call(`m${i}`, { big }),
    );
    const results = await Promise.all(calls);
    results.forEach((r, i) => assert.deepEqual(r, { echo: `m${i}` }));
    assert.deepEqual(failures, []);
  } finally {
    await rpc.close();
  }
});

test("writes after a real transport failure still reject with TransportLoss", async () => {
  const { rpc, failures } = echoProcess();
  await rpc.close();
  await assert.rejects(rpc.notify("late"), TransportLoss);
  assert.equal(failures.length, 1);
});
