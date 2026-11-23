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
import { existsSync, readdirSync, readFileSync } from "node:fs";
import { join, relative } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL("../../", import.meta.url));
const openapi = JSON.parse(readFileSync(join(root, "backend/openapi.json"), "utf8"));

// Calls with no equivalent backend route today. Each needs a product decision
// (rebuild against a real endpoint, or retire the job), not a path rename.
// Keep this list exact: an entry that starts matching, or a new missing call,
// fails the test. (github-push and embedding-regeneration were retired in
// GOO-678 for this reason.)
const KNOWN_MISSING = new Map();

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

/**
 * Segment-wise match, no regex built from route text. A `{name}` route segment
 * matches any one segment; every other segment must be equal. A caller's
 * `{param}` (a template expression) therefore only matches a route parameter.
 */
export function pathMatches(route, path) {
  const want = route.split("/");
  const got = path.split("/");
  return (
    want.length === got.length &&
    want.every((seg, i) => /^\{[^}]+\}$/.test(seg) || seg === got[i])
  );
}

function routeExists(method, path) {
  return Object.entries(openapi.paths).some(
    ([route, ops]) => pathMatches(route, path) && method.toLowerCase() in ops,
  );
}

const found = [];
const taskIds = new Set();
for (const file of tsFiles(join(root, "src/trigger"))) {
  for (const m of readFileSync(file, "utf8").matchAll(/^\s*id:\s*["']([\w-]+)["']/gm)) {
    taskIds.add(m[1]);
  }
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

// A Next route that triggers a task by id over the Trigger.dev REST API is
// invisible to the backend-path scan. Retiring a task must also retire its
// callers (GOO-678: the GitHub webhook kept triggering github-push-reindex).
function* sourceFiles(dir) {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    if (entry.name === "node_modules" || entry.name.startsWith(".")) continue;
    const path = join(dir, entry.name);
    if (entry.isDirectory()) yield* sourceFiles(path);
    else if (/\.(ts|tsx|mjs|js)$/.test(entry.name) && !/\.test\./.test(entry.name)) {
      yield path;
    }
  }
}

const TASK_CALLER_DIRS = ["frontend/app", "frontend/src", "src", "terminal/src", "packages"];

function taskCallers() {
  const callers = [];
  for (const dir of TASK_CALLER_DIRS) {
    if (!existsSync(join(root, dir))) continue;
    for (const file of sourceFiles(join(root, dir))) {
      const text = readFileSync(file, "utf8");
      const patterns = [
        /\/api\/v1\/tasks\/([\w-]+)\/(?:trigger|batch)/g,
        /tasks\.(?:trigger|batchTrigger|triggerAndWait)\s*(?:<[^>]*>)?\(\s*["']([\w-]+)["']/g,
      ];
      for (const re of patterns) {
        for (const m of text.matchAll(re)) callers.push({ id: m[1], file: relative(root, file) });
      }
    }
  }
  return callers;
}

test("every Trigger.dev task triggered by id is defined in src/trigger", () => {
  assert.ok(taskIds.has("execute-agent") && taskIds.has("batch-process-documents"));
  const callers = taskCallers();
  // Guards the scanner: the agent and batch routes trigger tasks by id.
  assert.ok(callers.length >= 2, `only ${callers.length} task callers found`);
  const orphans = callers.filter((c) => !taskIds.has(c.id)).map((c) => `${c.id} (${c.file})`);
  assert.deepEqual(orphans, [], `callers of undefined tasks:\n${orphans.join("\n")}`);
});

test("route matching compares whole segments", () => {
  assert.ok(pathMatches("/api/v1/agent/jobs/{job_id}", "/api/v1/agent/jobs/{param}"));
  assert.ok(pathMatches("/api/v1/agent/jobs/{job_id}", "/api/v1/agent/jobs/abc"));
  assert.ok(!pathMatches("/api/v1/workers/status", "/api/v1/workers/status/x"));
  assert.ok(!pathMatches("/api/v1/a.b", "/api/v1/axb"));
  assert.ok(!pathMatches("/api/v1/x", "/api/v1/{param}"));
});
