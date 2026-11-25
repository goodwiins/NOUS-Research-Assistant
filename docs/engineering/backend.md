# Backend standards

## Router → service → transaction

The former 2,517-line `backend/src/api/threads/workspaces.py` is now a
32-line compatibility shim: a docstring plus re-exports of `router` /
`standalone_router` and five more names
(`THREAD_PREVIEW_MAX_CHARS`, `create_workspace`, `add_workspace_member`,
`update_message_feedback`, `update_message_standalone`) that only
characterization/regression tests still import from this path directly
(`test_workspace_route_contract.py`, `test_workspace_tenant_scope.py`,
`test_workspace_member_readd.py`, `test_message_feedback_edit_guard.py`,
`test_create_message_route_deprecation.py`) — all seven are load-bearing,
not dead re-exports; the module's `__all__` is the definitive list. The
real layout:

- **Routers** (transport plus the handler-end commit):
  `backend/src/api/threads/workspace_routes/{workspaces,members,conversations,threads,messages,collections}.py`, plus
  two shared modules — `dependencies.py` (scope/access, raises
  `HTTPException`) and `presenters.py` (response shaping). Composed, in the
  original route-registration order, by `workspace_routes/__init__.py` into
  the two exported routers.
- **Services** (persistence, flush-only): `backend/src/services/threads/
  {workspace,conversation,thread,message,collection}_service.py`,
  `workspace_access.py` (the one scope/access funnel), and `chat_service.py`
  (`ChatService` — a compatibility facade; see below). The five leaf
  services never call `.commit()`; `ChatService` commits only in the
  functions listed in the test's `CHAT_SERVICE_COMMIT_ALLOWLIST`.

Enforced by `backend/tests/unit/architecture/test_workspace_boundaries.py`:

1. Each resource route module (`workspaces`, `members`, `conversations`,
   `threads`, `messages`, `collections`) owns the request commit: it calls
   `.commit()` at least once, each handler calls it at most once, and it
   never calls `.rollback()` / `.flush()` / `.refresh()` (rollback stays
   with the request-scoped session). The shared modules (`dependencies.py`,
   `presenters.py`, `__init__.py`) call none of the four. The test scans
   only `workspace_routes/`; other `src/api/` modules commit directly and no
   test constrains them.
2. No resource module imports a sibling resource module directly — cross-
   handler helpers go through `dependencies.py` or `presenters.py` only.
3. `workspace_routes/__init__.py` and the `workspaces.py` shim resolve
   `router`/`standalone_router` to the same objects.
4. The five leaf services above call no `.commit()`, and `chat_service.py`
   commits only in `CHAT_SERVICE_COMMIT_ALLOWLIST` (with no stale entries).

**Named exception, not a loophole**: `workspace_routes/messages.py`'s
`create_message`/`create_message_standalone` re-issue a read-only
`select(ChatMessage).options(selectinload(...))` after delegating the write
to `ChatService.create_message`, to eager-load citation/attachment metadata
for the response. That's a read, not a transaction boundary.

## Tenant / access scope

- Workspace access is **membership-based**, not organization-based:
  `workspace_access.user_can_access_workspace` = not deleted AND (public OR
  member OR owner). A workspace's `organization_id` is metadata recorded at
  creation, not an access filter — a public workspace is visible cross-org
  by design.
- Every `workspace_access.get_*` getter (`get_workspace`, `get_conversation`,
  `get_thread`, `get_thread_lean`, `get_message`, `get_collection`) requires
  `user_id` with no default and fails closed (returns `None`); router callers
  turn that into
  404 via `workspace_routes/dependencies.py`, `ChatService` uses the
  `Optional[...]` directly. Enforced by
  `backend/tests/unit/architecture/test_maintenance_contracts.py`.
- **Document access is organization-scoped** (per the repo-wide tenant
  rule): `workspace_access.get_accessible_document_or_none(db, document_id,
  user_id, organization_id)` filters `Document.organization_id` — every
  document / content-hash-dedup / search-suggestion query must filter
  `organization_id`, the same way.
- Soft-deletes don't cascade to child rows, only to access: deleting a
  workspace/conversation/thread stamps only that row's own `is_deleted`.
  `get_conversation`/`get_thread`/`get_message` each re-check every
  ancestor's `is_deleted` on every fetch — a `Workspace.is_deleted = True`
  must still revoke access to its conversations/threads/messages even
  though no cascade touched those rows.

## Compatibility-preserving consolidation

Where a router historically diverged from `ChatService` (measured during
the Task 4.3 consolidation — see the plan's amendment A2), the router's
endpoint keeps its own behavior, and `ChatService`'s pre-existing callers
keep their old behavior via an explicit flag defaulting to "old":
`workspace_service.create_workspace(..., enforce_org_match=True|False)`,
`list_workspaces(..., filter_deleted_memberships=True|False)`,
`delete_workspace(..., stamp_deleted_at=True|False)`. Read a service
function's docstring before assuming its default matches every caller —
they usually don't, on purpose.

Keep a compatibility export until every caller has migrated (see
`backend/src/api/threads/workspaces.py`); removing one is a separate,
deliberate change, not a side effect of an unrelated refactor.

## Thread message reads

Chat reads never load a whole thread's messages:

- `GET /api/v2/threads/{thread_id}` returns at most the newest 500 live
  messages (`THREAD_DETAIL_MAX_MESSAGES` in `thread_service.py`), oldest
  first. `has_more_messages` is `true` when older live messages exist; page
  them through `GET /api/v2/threads/{thread_id}/messages?order=desc&before_id=<oldest returned id>`.
- `ChatService.get_thread_context` reads only the newest `max_messages`
  live rows (`message_service.list_messages(order="desc", limit=...)`), and
  takes `total_messages` from that query's count.

## Ratchets

- **Ruff `F821`/`F823`** (undefined name / used-before-assignment) are
  blocking, repo-wide: `ruff check backend/src`. These are latent
  `NameError`s, not style — the rest of the default rule set is either
  ignored (see `pyproject.toml`'s `[tool.ruff.lint]`) or advisory.
- **Changed-file gate** (CI `lint-backend`, blocking): every changed
  `.py` file (`scripts/ci/changed_source_files.py --kind python`) must pass
  `ruff check`, `black --check`, `isort --check-only`. *Added* files only
  (`--kind python-added`) additionally run
  `mypy --ignore-missing-imports --follow-imports=silent` — modified legacy
  files carry too much pre-existing type debt (measured: 529 errors across
  one 27-file sample) to gate on until that debt is paid down.
- Full-tree Black/isort/MyPy stay **advisory**
  (`continue-on-error: true` in CI) until their debt reaches zero — see
  [testing.md](testing.md) for how to move a floor.
- `.pre-commit-config.yaml`'s local hooks run the *exact same* pinned
  `ruff`/`black`/`isort` commands as CI, staged-file scoped — never a
  separately-pinned mirror. `scripts/ci/check_tool_versions.py` (+
  `backend/tests/unit/ci/test_tool_versions.py`) fails if they drift apart.

## Commands

Run every blocking gate across the branch before pushing —
`scripts/ci/run_local_ci.sh` (add `--frontend` for the pnpm checks). It
ratchets changed files against the base branch the way CI does, which
`.pre-commit-config.yaml` cannot: pre-commit only sees the files being staged,
so one formatted in an earlier commit and edited later still fails CI.
Untracked files that are not gitignored count as changed (and as added, for
mypy), so a new file is ratcheted before its first `git add`. Backend tests
run from the repo root with `-c backend/pytest.ini` and the unit job's
placeholder env, as CI runs them; `backend/tests/unit/architecture` runs even
with `--skip-tests`. `--frontend` also runs `pnpm test:coverage` and the
coverage ratchet (`scripts/ci/check_frontend_coverage.mjs`) unless
`--skip-tests` is given. `backend/tests/unit/ci/test_run_local_ci_parity.py`
pins these against `.github/workflows/test-pipeline.yml`.

```sh
scripts/ci/run_local_ci.sh              # blocking gates + unit tests
scripts/ci/run_local_ci.sh --skip-tests # gates + architecture tests only

ruff check backend/src
black --check backend/src
isort --check-only backend/src
mypy backend/src --ignore-missing-imports   # advisory, full-tree baseline
pytest -q backend/tests/unit/architecture backend/tests/unit/api
pytest -q backend/tests/unit/services/threads backend/tests/api/threads
python scripts/ci/generate_openapi.py --check
```

## Python dependencies

- `backend/requirements.txt` is the production set.
  `backend/requirements-cpu.in` includes it and adds the CPU torch wheel, so
  it is the source of `backend/requirements-cpu-lock.txt`. After editing it, regenerate
  the lock from the repo root without `--upgrade`, so existing pins are kept:

  ```sh
  uv pip compile --python-version 3.11 --python-platform x86_64-manylinux_2_28 --no-annotate backend/requirements-cpu.in -o backend/requirements-cpu-lock.txt
  ```

- `backend/requirements-ci.txt` holds the test tools CI installs next to the
  lock (pytest and its plugins, testcontainers).
- `backend/requirements.dev.txt` is the local dev set: `requirements.txt`,
  `requirements-ci.txt` and the lint/dev tools.
- Test and profiling tools never go in `requirements.txt`. Add a package there
  only when runtime code imports it, or when it is a documented CLI, driver or
  security-floor pin (for example `flower`, `gunicorn`, `aiosqlite`).
  `backend/tests/unit/ci/test_requirements_layout.py` guards this layout.
