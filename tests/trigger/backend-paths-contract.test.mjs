// GOO-678: every backend path a Trigger.dev task calls must exist, with that
// method, in the committed OpenAPI snapshot (backend/openapi.json). The
// queue-depth monitor called /api/v1/infrastructure/workers/status, which is
// not a route (the router is mounted at /api/v1/workers), so every run failed
// and sent a Slack alert every five minutes.
//
// This is a static scan of src/trigger/**/*.ts for backendClient.get/post
// calls with a literal or template path. Template expressions become a path
// parameter; a query string is ignored.
// Run: node --experimental-transform-types --experimental-test-module-mocks \
//        --test tests/trigger/*.test.mjs
import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { join, relative } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL("../../", import.meta.url));
const openapi = JSON.parse(readFileSync(join(root, "backend/openapi.json"), "utf8"));

// Calls with no equivalent backend route today. Each needs a product decision
// (rebuild against a real endpoint, or retire the job), not a path rename.
// Keep this list exact: an entry that starts matching, or a new missing call,
// fails the test.
const KNOWN_MISSING = new Map([
  [
    "POST /api/v2/documents/upload/single",
    "github-push: there is no JSON upload route; the only upload is multipart POST /api/v1/files/upload",
  ],
  [
    "GET /api/v1/search/vectors/model-info",
    "embedding-regeneration: the vector-store routes were removed",
  ],
  [
    "POST /api/v1/search/vectors/regenerate",
    "embedding-regeneration: the vector-store routes were removed",
  ],
]);

function* tsFiles(dir) {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) yield* tsFiles(path);
    else if (entry.name.endsWith(".ts")) yield path;
  }
}

/** All backendClient.get/post calls with their first (path) argument. */
export function backendCalls(source) {
  const calls = [];
  const head = /backendClient\s*\.\s*(get|post)\b\s*/g;
  for (const m of source.matchAll(head)) {
    let i = m.index + m[0].length;
    // Skip an explicit type argument, however deeply its <...> nest.
    if (source[i] === "<") {
      let depth = 0;
      for (; i < source.length; i++) {
        if (source[i] === "<") depth++;
        else if (source[i] === ">" && source[i - 1] !== "=" && --depth === 0) break;
      }
      i++;
    }
    const rest = source.slice(i).match(/^\s*\(\s*(["'`])((?:(?!\1)[\s\S])*?)\1/);
    if (rest) calls.push({ method: m[1].toUpperCase(), raw: rest[2] });
  }
  return calls;
}

export function normalize(raw) {
  return raw.replace(/\$\{[^}]*\}/g, "{param}").split("?")[0];
}

function routeExists(method, path) {
  return Object.entries(openapi.paths).some(([route, ops]) => {
    const pattern = new RegExp(
      "^" + route.replace(/\{[^}]+\}/g, "[^/]+").replace(/[.]/g, "\\.") + "$",
    );
    return pattern.test(path.replace(/\{param\}/g, "x")) && method.toLowerCase() in ops;
  });
}

const found = [];
for (const file of tsFiles(join(root, "src/trigger"))) {
  for (const call of backendCalls(readFileSync(file, "utf8"))) {
    found.push({ ...call, path: normalize(call.raw), file: relative(root, file) });
  }
}

test("the scan finds the trigger tasks' backend calls", () => {
  // Guards the scanner itself: a regex that matches nothing would pass vacuously.
  assert.ok(found.length >= 12, `only ${found.length} calls found`);
  assert.ok(found.some((c) => c.path === "/api/v1/arxiv/search"));
  assert.ok(found.some((c) => c.path === "/api/v1/agent/jobs/{param}"));
});

test("every backend path a trigger task calls exists in openapi.json", () => {
  const missing = found
    .filter((c) => !routeExists(c.method, c.path))
    .map((c) => `${c.method} ${c.path}`)
    .filter((key) => !KNOWN_MISSING.has(key));
  assert.deepEqual(missing, [], `missing routes:\n${missing.join("\n")}`);
});

test("the known-missing list holds only calls that are still made and still missing", () => {
  for (const key of KNOWN_MISSING.keys()) {
    const [method, path] = key.split(" ");
    assert.ok(
      found.some((c) => c.method === method && c.path === path),
      `${key} is no longer called; drop it from KNOWN_MISSING`,
    );
    assert.ok(!routeExists(method, path), `${key} now exists; drop it from KNOWN_MISSING`);
  }
});

test("the scanner normalizes templates and query strings", () => {
  const calls = backendCalls(
    'backendClient.get<{ a: number }>(`/api/v1/x/${id}?y=1`); backendClient.post("/b", {});' +
      "backendClient.get<{\n w?: Array<Record<string, unknown>>; f?: (x: number) => void;\n}>(\"/c\")",
  );
  assert.deepEqual(
    calls.map((c) => [c.method, normalize(c.raw)]),
    [
      ["GET", "/api/v1/x/{param}"],
      ["POST", "/b"],
      ["GET", "/c"],
    ],
  );
});
