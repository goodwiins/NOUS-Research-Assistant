import assert from "node:assert/strict";
import { test } from "node:test";
import { chmodSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { CredentialStore } from "../src/credentials.ts";
import { pruneIdleAdapters } from "../src/cli.ts";
import type { HarnessAdapter } from "../src/contracts.ts";

// TD2D-15 / TD2D-17 (GOO-657).
// Command: pnpm --filter @nous/harness-bridge test test/local-state-lifecycle.test.ts

test("a missing connection file reads as null", async (t) => {
  const dir = mkdtempSync(join(tmpdir(), "nous-state-"));
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  const store = new CredentialStore(dir);
  assert.equal(await store.readLocalIfPresent("connection"), null);
});

test("an unreadable connection file is an error, not 'not connected'", async (t) => {
  const dir = mkdtempSync(join(tmpdir(), "nous-state-"));
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  const store = new CredentialStore(dir);
  await store.writeLocal("connection", { apiUrl: "x" });
  // Corrupt the stored JSON in place (owner-only mode preserved).
  const file = join(dir, "connection.json");
  writeFileSync(file, "{not json");
  chmodSync(file, 0o600);
  await assert.rejects(
    store.readLocalIfPresent("connection"),
    /stored connection is unreadable/,
  );
});

function fakeAdapter(log: string[], name: string, fail = false): HarnessAdapter {
  return {
    async closeSession() {
      log.push(name);
      if (fail) throw new Error("close failed");
    },
  } as unknown as HarnessAdapter;
}

test("idle adapters are closed and forgotten; locked runs keep theirs", async (t) => {
  t.mock.method(console, "error", () => {});
  const closed: string[] = [];
  const adapters = new Map<string, HarnessAdapter>([
    ["run-done", fakeAdapter(closed, "run-done", true)],
    ["run-live", fakeAdapter(closed, "run-live")],
    ["run-old", fakeAdapter(closed, "run-old")],
  ]);
  const journal = {
    activeCommands: () => [{ runId: "run-live" }] as never,
  };
  await pruneIdleAdapters(adapters, journal);
  // A failing close does not stop the others.
  assert.deepEqual(closed.sort(), ["run-done", "run-old"]);
  assert.deepEqual([...adapters.keys()], ["run-live"]);
});
