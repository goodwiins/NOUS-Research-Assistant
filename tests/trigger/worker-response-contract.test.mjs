// GOO-718: the zod schemas the scheduled tasks parse worker responses with
// must match a typed response model in the committed OpenAPI snapshot
// (backend/openapi.json). GET /api/v1/workers/health had no response_model, so
// its OpenAPI schema was `{}` and a backend shape change could not fail CI;
// the health monitor would only find out at run time, as a ZodError.
//
// For each route: the 200 response must be a named component schema, and every
// field the zod schema requires must be a required property of that component
// with a compatible JSON type.
// Run: node --experimental-transform-types --experimental-test-module-mocks \
//        --test tests/trigger/*.test.mjs
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import { z } from "zod";

import {
  WorkerHealthResponseSchema,
  WorkerStatsResponseSchema,
} from "../../src/trigger/_lib/schemas.ts";

const root = new URL("../../", import.meta.url);
const openapi = JSON.parse(readFileSync(new URL("backend/openapi.json", root)));

const CONTRACTS = [
  ["/api/v1/workers/health", WorkerHealthResponseSchema],
  ["/api/v1/workers/status", WorkerStatsResponseSchema],
];

function resolve(schema) {
  const ref = schema?.$ref;
  if (!ref) return schema;
  return openapi.components.schemas[ref.replace("#/components/schemas/", "")];
}

/** Does a value valid against backend type `have` always satisfy zod type `want`? */
export function compatible(want, have) {
  const h = resolve(have);
  if (!want?.type || !h?.type) return false;
  if (want.type === "number") return h.type === "number" || h.type === "integer";
  if (want.type === "array") {
    return h.type === "array" && compatible(want.items, h.items);
  }
  return want.type === h.type;
}

for (const [path, zodSchema] of CONTRACTS) {
  test(`${path} returns a typed model that satisfies the task's zod schema`, () => {
    const declared =
      openapi.paths[path]?.get?.responses?.["200"]?.content?.["application/json"]
        ?.schema;
    assert.ok(declared, `${path} has no GET 200 JSON response`);
    assert.match(
      declared.$ref ?? "",
      /^#\/components\/schemas\/\w+$/,
      `${path} 200 response is untyped (${JSON.stringify(declared)}); give the route a response_model`,
    );
    const model = resolve(declared);
    const want = z.toJSONSchema(zodSchema);
    for (const field of want.required) {
      assert.ok(
        model.required?.includes(field),
        `${path}: zod requires "${field}" but the backend model does not`,
      );
      assert.ok(
        compatible(want.properties[field], model.properties[field]),
        `${path}: "${field}" is ${JSON.stringify(want.properties[field])} in zod, ` +
          `${JSON.stringify(model.properties[field])} in the backend model`,
      );
    }
  });
}

test("type compatibility is strict where it matters", () => {
  assert.ok(compatible({ type: "number" }, { type: "integer" }));
  assert.ok(!compatible({ type: "integer" }, { type: "number" }));
  assert.ok(!compatible({ type: "boolean" }, { type: "string" }));
  assert.ok(!compatible({}, {}));
  assert.ok(
    compatible(
      { type: "array", items: { type: "string" } },
      { type: "array", items: { type: "string" } },
    ),
  );
  assert.ok(
    !compatible(
      { type: "array", items: { type: "string" } },
      { type: "array", items: { type: "object" } },
    ),
  );
});
