# AGENTS.md

Backend-specific guidance. The repository-root `AGENTS.md` still applies.

## Scope and sources of truth

The backend owns FastAPI transport, thread services, persistence boundaries,
and the backend half of the API contract. Use these contracts as the source of
truth before changing code:

- [Backend standards](../docs/engineering/backend.md)
- [API contracts](../docs/engineering/api-contracts.md)
- [Testing standards](../docs/engineering/testing.md)
- [Operational gotchas](../docs/engineering/gotchas.md)
- [Invalid-pattern audit](../docs/audits/2026-09-14-invalid-patterns-audit.md)

The audit's IP-001 and IP-002 are current evidence to preserve against; RR-001
remains a reachability question, not permission to broaden an exception.

## Invalid patterns

- In `backend/src/api/threads/workspace_routes/`, the route handler owns the
  request's single commit: a handler calls `await db.commit()` at most once,
  at the end of a write, and never calls `rollback`/`flush`/`refresh`. The
  leaf services under `backend/src/services/threads/` do the writes but only
  flush; they never commit. `ChatService` commits only in the functions in
  `CHAT_SERVICE_COMMIT_ALLOWLIST`, so a handler that delegates its write to
  one of them, such as `create_message_standalone`
  (`ChatService.create_message`), must not commit: a route commit there would
  double-commit. `get_db` does not commit, so a write handler that leaves out
  its commit returns 2xx and is rolled back.
- `test_workspace_boundaries.py` enforces part of that rule: at most one
  commit per handler, at least one per resource route module, no
  `rollback`/`flush`/`refresh` in a route module, no commit in the five leaf
  services, and `ChatService` commits only in its allowlist. A static walk
  cannot tell which handlers write, so it checks only the per-module floor
  and does not catch a write handler that leaves out its commit while another
  handler in the module commits. It reads only the calls written in the route
  module, so it also misses a route commit added after a committing
  `ChatService` call.
- Routers under `backend/src/api/agent/` follow the opposite rule: they never
  commit, roll back, open their own sessions, or reference
  thread/workspace columns; services own the transaction and
  `workspace_access` owns scope. `test_agent_api_boundaries.py` enforces this,
  and its allowlist only shrinks.
- Treat `backend/src/services/threads/workspace_access.py` as the access
  funnel. Workspace access is membership/public/owner based, not organization
  based. Document, content-hash deduplication, and search-suggestion access is
  organization scoped.
- A child getter must re-check every soft-deleted ancestor on every fetch;
  soft-deleting a parent must revoke child access even without a database
  cascade. Access helpers must fail closed when caller identity is absent.
- Public HTTP errors must use stable safe messages. Do not put arbitrary
  internal exception text in a response; log useful structured context
  internally without disclosing secrets or implementation details.
- Bind SQL values or use validated enums for identifiers accepted from callers;
  never interpolate an untrusted sort or filter value into SQL.
- Keep compatibility exports and behavior flags while callers still depend on
  them. Remove or change them only as a deliberate, caller-audited change, not
  as an incidental refactor.

## Required workflow

- Keep the router → service boundary intact. In the workspace/thread API, the
  five leaf services do the writes (flush only) and the route handler commits
  at most once. Agent routers, and other routers by default, delegate the
  transaction to a service that commits it (for example
  `agent_run_service.upsert_run`). Route-level response shaping and a
  documented read-only reload are not substitutes for service-owned writes.
- For an HTTP schema change, regenerate `backend/openapi.json` from FastAPI and
  regenerate `frontend/src/types/generated/api.d.ts` in the same change. Never
  hand-edit either generated artifact.
- Preserve the organization predicates and ancestor soft-delete checks when
  touching document/status paths, and add negative authorization coverage for
  foreign or deleted records when the operation changes.

## Verification

Run only the matrix-backed checks below for routine backend validation:

```sh
ruff check backend/src
pytest -q backend/tests/unit/architecture backend/tests/unit/api
pytest -q backend/tests/unit/services/threads backend/tests/api/threads
python scripts/ci/generate_openapi.py --check
```

Broader database, service, or end-to-end integration checks require their
external prerequisites; report them as service-dependent and `NOT RUN` when
those prerequisites are unavailable rather than implying they passed.
