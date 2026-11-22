// GOO-670: the bulk-ingest task must parse what POST /api/v1/arxiv/search
// really returns. The endpoint's response_model is List[ArXivPaperResponse]
// (a bare JSON array), but the task read `searchResult.papers`, so every
// bulk ingest started no children and finished with total 0.
//
// fixtures/arxiv-search-response.json was serialized with the backend's own
// route response_model (pydantic TypeAdapter over the /search route); the
// backend test backend/tests/unit/api/test_arxiv_search_contract_fixture.py
// fails if the endpoint stops producing that shape.
// Run: node --experimental-transform-types --experimental-test-module-mocks \
//        --test tests/trigger/*.test.mjs
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import {
  ArxivSearchResponseSchema,
  ArxivBulkIngestPayloadSchema,
} from "../../src/trigger/_lib/schemas.ts";

const root = new URL("../../", import.meta.url);
const fixture = JSON.parse(
  readFileSync(new URL("tests/trigger/fixtures/arxiv-search-response.json", root)),
);

test("the search schema parses the real /arxiv/search response", () => {
  const papers = ArxivSearchResponseSchema.parse(fixture);
  assert.deepEqual(
    papers.map((p) => p.id),
    ["2301.07041v2", "hep-th/9901001v1"],
  );
});

test("an object-wrapped response is rejected instead of yielding zero papers", () => {
  assert.throws(() => ArxivSearchResponseSchema.parse({ papers: fixture }));
});

test("the committed OpenAPI snapshot still declares a bare array", () => {
  const openapi = JSON.parse(readFileSync(new URL("backend/openapi.json", root)));
  const schema =
    openapi.paths["/api/v1/arxiv/search"].post.responses["200"].content[
      "application/json"
    ].schema;
  assert.equal(schema.type, "array");
  assert.equal(schema.items.$ref, "#/components/schemas/ArXivPaperResponse");
});

test("the bulk payload schema is unaffected", () => {
  const payload = ArxivBulkIngestPayloadSchema.parse({ query: "rag", userId: "u" });
  assert.equal(payload.batchSize, 10);
});
