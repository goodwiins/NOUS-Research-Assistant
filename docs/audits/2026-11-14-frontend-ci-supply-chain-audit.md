# Frontend, CI and supply-chain audit, 2026-11-14

Read-only audit of `develop` @ `d06b1d01a` (2026-11-14), covering the two areas the [2026-11-13 audit](https://github.com/goodwiins/NOUS-Research-Assistant/pull/2123) only passed over lightly: the frontend, and CI plus supply chain. No application code, workflow, setting, PR, workflow run, cluster or database was changed. This is a dated record: re-verify a finding against current code before acting on it.

[Audits index](README.md) · [Current engineering contracts](../engineering/README.md)

## Method and coverage

- Frontend: static reading of `frontend/`, `packages/chat-runtime` and (briefly) `terminal/`. `pnpm install --frozen-lockfile` and `pnpm --dir frontend exec tsc --noEmit` were run; no build, dev server, Vitest or Playwright run. Stream and store findings come from reading the full code path; none was reproduced in a browser.
- CI: every file in `.github/workflows/`, plus read-only `gh api` GET calls for repository settings, rulesets, runners, secret names (never values), code-scanning and Dependabot alerts, and the last 200 Test Pipeline runs (2026-10-29 to 2026-11-14) with job logs for the failures.
- Not covered: `useChatSession.ts`, the submit preflight and confirm-stream body of `useChatStreaming.ts` (`:2170-2600`, `:3160-3700`), `projectStore`/`projectChatStore`/`pipelineStore`, bundle sizes (no build was run), and a full sweep of swallowed `catch` blocks in components.
- **UNVERIFIED** marks anything not confirmed. Effort: S is under a day, M is one to three days, L is a week or more.
- Not repeated here because they are fixed or in flight: #2126, #2127, #2128, #2132, #2133.

## Top 10, ranked by impact × likelihood

### 1. `develop` accepts force pushes and unreviewed merges, and GitHub's free protections are still off

- **Severity**: high
- **Evidence** (`gh api`, 2026-11-14)
  - `repos/.../branches/develop/protection`: `"allow_force_pushes":{"enabled":true}`, `"enforce_admins":{"enabled":false}`, `"strict":false`, no `required_pull_request_reviews` key, required contexts `["Release Gate"]` only. Force pushes were not reported on 2026-11-13.
  - `repos/.../rulesets/23197379` ("develop required checks", active): `"rules":[]`. `repos/.../rules/branches/develop` returns `[]`.
  - `repos/...` `security_and_analysis`: `secret_scanning`, `secret_scanning_push_protection` and `dependabot_security_updates` all `disabled`; `automated-security-fixes` is `{"enabled":false}`. Visibility is `public`.
  - `actions/permissions`: `allowed_actions: all`, `sha_pinning_required: false`. `actions/permissions/workflow`: `can_approve_pull_request_reviews: true`.
  - `environments`: `Preview` and `Production` have no protection rules and no branch policy.
- **Failure scenario**: a force push to `develop` rewrites the branch Argo CD tracks and that `release-dev.yml` builds from; nothing blocks it or records a review. A pushed credential is neither blocked nor flagged.
- **Fix**: turn off force pushes on `develop`, enforce for admins, require one review; put the required check in the ruleset or delete the empty ruleset; enable secret scanning, push protection and Dependabot security updates; require SHA pinning (all `uses:` are already pinned).
- **Effort**: S (settings only; needs an owner)

### 2. 70 open CodeQL alerts, 58 of them high, and nothing gates on them

- **Severity**: high
- **Evidence**
  - `gh api "repos/.../code-scanning/alerts?state=open&per_page=100"`: 70 open (58 high, 12 medium). 40 were created on 2026-11-09, when default setup was re-enabled (`code-scanning/default-setup`: `state: configured`, weekly).
  - By rule: `py/clear-text-logging-sensitive-data` 15, `py/weak-sensitive-data-hashing` 12, `py/path-injection` 10, `py/stack-trace-exposure` 10, `py/clear-text-storage-sensitive-data` 4, `py/sql-injection` 3, `py/insecure-protocol` 3.
  - Locations worth reading first: `py/sql-injection` ×3 in `backend/src/services/quality/user_behavior_service.py`; `py/path-injection` in `backend/src/services/arxiv/arxiv_service.py` (4), `backend/src/services/research_engine/blueprints/loader.py` (3), `backend/src/api/analytics/reports.py` (2), `backend/src/services/artifacts/storage.py` (1). Frontend: `js/bad-tag-filter` in `frontend/src/security/frontendSecurity.ts`, `js/redos` in `frontend/src/hooks/useChatPersistence.ts`, `js/insecure-randomness` in `frontend/src/lib/analytics.ts` and `frontend/src/utils/errorTracking.ts`.
  - CodeQL is not a required check (item 1), so a PR that adds an alert merges. Whether each alert is exploitable is UNVERIFIED; none was triaged here.
- **Failure scenario**: a real injection or path-traversal finding sits in a list nobody is forced to read, and new ones accumulate.
- **Fix**: triage the 13 SQL-injection and path-injection alerts first (fix or dismiss with a reason); then require the CodeQL check for new high alerts on PRs.
- **Effort**: M

### 3. The browser test gate is 3 smoke tests on a PR and 28 tests after merge; 33 specs never run

- **Severity**: high
- **Evidence**
  - `.github/workflows/test-pipeline.yml:1571-1579`: PRs run `playwright test --grep "@smoke" --project=chromium`; pushes run the chromium project. Both use `tests/e2e` only.
  - Run [43613898827](https://github.com/goodwiins/NOUS-Research-Assistant/actions/runs/43613898827) (develop push, "full"): `Running 28 tests using 1 worker`, `27 passed (1.1m)`, `1 skipped`.
  - `tests/e2e/playwright.config.ts:81-97`: the chromium project ignores `accessibility/**`, `visual/**`, `performance/**`, `mobile-responsive/**`, `data-flow/**` and `custom-dashboard-creation`. No workflow selects the `accessibility`, `visual-regression` or `performance` projects.
  - `grep -rn -E 'frontend/e2e|test:e2e' .github/workflows` returns nothing. `frontend/e2e` holds 33 spec files and `frontend/` has 13 `playwright.*.config.ts` files, including `nous-flows/chat-terminal-reconciliation.spec.ts`, the browser proof for the stream reconciliation in items 4 and 8.
  - The full lane runs only on a `develop` push, and 36 of the last 51 push runs were cancelled (item 5).
- **Failure scenario**: a chat, upload or accessibility regression passes every PR check and is first seen in live dev.
- **Fix**: run a credential-free subset of `frontend/e2e` (chat send, reconciliation, artifact panel) and the `accessibility` project in the PR lane; delete or archive specs that cannot run without live keys.
- **Effort**: M

### 4. A resumed chat stream rebuilds only the tail of the answer

- **Severity**: medium-high
- **Evidence** (read end to end, not reproduced)
  - `frontend/src/hooks/chat/useChatStreaming.ts:1237`: every turn starts with `let assistantContent = '';`.
  - `:2783-2785`: resume calls `agentChatService.resumeStream(threadId, Math.max(latestRun.streamSeq ?? 0, pendingSeq), …)`, so only frames after the saved cursor arrive.
  - `:2737` `suppressCommit: () => replayGapSeen`, and the gap fires only when `seq > afterSeq + 1` (`frontend/src/services/agentChatService.ts:932`). A clean replay from cursor N > 0 is not a gap, so the tail is committed.
  - `frontend/src/hooks/__tests__/useChatStreaming.resume.test.tsx:99-111` asserts the cursor value, never the content.
- **Failure scenario**: 400 tokens stream, the connection drops (or the user leaves `/chat` and returns), and the resume turn starts from cursor N. The bubble restarts empty and shows only what came after N; earlier tool steps, plan and citations are gone. It heals only when `refreshMessages` finds the persisted message; if that refresh fails the truncated answer stays until reload.
- **Fix**: resume with `after=0` when the closure holds no prior content, or treat a start cursor above 0 as `suppressCommit` and show a reconnecting state. Add a content assertion to the resume test.
- **Effort**: S

### 5. Most `develop` commits are never verified: 36 of 51 push runs were cancelled

- **Severity**: medium
- **Evidence**
  - `.github/workflows/test-pipeline.yml:12-14`: `group: ${{ github.workflow }}-${{ github.event.pull_request.number || github.ref }}` with `cancel-in-progress: true`, which also applies to pushes on `develop`.
  - Last 200 Test Pipeline runs: push events are 36 cancelled, 13 success, 1 failure, 1 in progress. On 2026-11-12, 11 consecutive push runs were cancelled before one completed ([43151721039](https://github.com/goodwiins/NOUS-Research-Assistant/actions/runs/43151721039)).
  - The push run is the only place the full E2E lane runs (item 3) and the only evidence `release-dev.yml:27-31` accepts.
  - PR runs use a lighter profile (`test-pipeline.yml:41-43` comment: "Selection is PR-only"), and `strict` is false, so a PR can merge on checks from a stale base.
- **Failure scenario**: during a merge burst the full suite runs once, on the last commit. If it fails, the culprit is one of up to a dozen commits with no per-commit signal.
- **Fix**: set `cancel-in-progress: ${{ github.event_name == 'pull_request' }}`, or adopt a merge queue so each merge is tested against the real base.
- **Effort**: S

### 6. Dependabot alerts grew from 42 to 53 in a day and nothing opens the fixes

- **Severity**: high
- **Evidence**
  - `gh api "repos/.../dependabot/alerts?state=open" --paginate`: 53 open (8 critical, 18 high, 23 medium, 4 low), against 42 on 2026-11-13.
  - New since then: `nltk` 10 alerts (7 high, patched in 3.10.4), `opentelemetry-api` 3 medium (patched in 1.45.2), `zod` 2 high (4.6.6).
  - Unchanged: `python-jose` 6 critical with no patched version; `tinypool` 2 critical; `vite` 10 across severities (patched in 6.4.4); `extract-zip` high and `deepmerge` low with no patch.
  - `dependabot_security_updates` is disabled (item 1), so a patched advisory waits for the weekly grouped PR. `test-pipeline.yml:806-808` runs `safety check` with `continue-on-error: true` and an unpinned `safety`; there is no `pip-audit` or `pnpm audit` step.
- **Failure scenario**: a patched high-severity advisory in a runtime dependency (`nltk`, `zod`) stays in the deployed image until someone reads the alert list.
- **Fix**: enable Dependabot security updates; bump `nltk`, `opentelemetry-api`, `vite`/`vitest` and `zod` now; migrate `python-jose` to `PyJWT` (already recommended on 2026-11-13).
- **Effort**: S for the bumps, M for `python-jose`

### 7. Two CI steps download and execute unverified binaries next to tokens

- **Severity**: medium
- **Evidence**
  - `.github/workflows/release-dev.yml:110-114`: `curl -fsSL ".../yq_linux_amd64" -o /usr/local/bin/yq` with no checksum, in the `promote` job that holds `contents: write`, `pull-requests: write` and `RELEASE_PR_TOKEN` (`:87-89`, `:126`).
  - `.github/actions/codecov-test-results/action.yml:43-48`: `curl -s -o codecovcli https://cli.codecov.io/latest/linux/codecov`, then executes it with the Codecov token. `latest` is mutable. The action is used at `test-pipeline.yml:531,874,1133,1222`.
  - The other downloads do verify: `supabase-migrations.yml:38-41` and `test-pipeline.yml:720-721` use `sha256sum -c`.
  - `docker-build.yml:111`: `provenance: false`; no SBOM, signature or image scan step exists in any workflow (`grep -n -E 'trivy|grype|sbom|cosign|attest'` returns only that line).
- **Failure scenario**: a compromised release asset or CDN response runs in the job that writes to `develop` and opens the deploy PR.
- **Fix**: pin a version and verify a SHA-256 for both binaries (the pattern is already in the repo); enable provenance or add an image scan to `docker-build.yml`.
- **Effort**: S

### 8. The chat store's stale-404 recovery never runs

- **Severity**: medium
- **Evidence**
  - `frontend/src/store/chat/slices/threadSlice.ts:95-96`: `const err = error as { response?: { status?: number } }; if (err?.response?.status === 404)`. Same shape at `conversationSlice.ts:91-92` and `:167-168`.
  - `frontend/src/services/workspaceService.ts:43` uses `@/services/api-client`, which throws `APIErrorClass` carrying `error.status_code` (`frontend/src/services/api-client.ts:832-834`); the class has no `.response` (`frontend/src/types/api.ts:35-36`).
  - The tests pass because they throw the axios shape by hand: `frontend/src/store/__tests__/chat-store-cache-cleanup.test.ts:313,335,428`.
- **Failure scenario**: a conversation is deleted in another tab or device; this tab gets a 404 on `loadThreads`, shows "Failed to load threads" and keeps the stale selection until a reload.
- **Fix**: one shared `getErrorStatus` helper (`useChatSession.ts:32-44` already reads both shapes); make the tests throw `APIErrorClass`.
- **Effort**: S

### 9. Backend dependencies are ranges with no lockfile; one upstream release failed 10 runs across 6 PRs

- **Severity**: medium
- **Evidence**
  - `backend/requirements.txt`: 94 requirement lines, 52 with `==`, 42 with `>=`. No lock or hash file exists (`ls backend | grep -i lock` is empty; `constraints-ci.txt` has 3 constraints).
  - On 2026-11-04 and 11-05, `backend/tests/unit/agent/test_confirmation_service.py::test_old_receipt_cannot_approve_next_interrupt_in_same_node` failed in 10 runs on 6 unrelated branches (for example [41964343032](https://github.com/goodwiins/NOUS-Research-Assistant/actions/runs/41964343032), [41958027162](https://github.com/goodwiins/NOUS-Research-Assistant/actions/runs/41958027162)) until #2070 raised the LangGraph floor (`backend/requirements.txt:125`).
  - `docker-build.yml:104-116` builds the deployed image from the same file, so two builds of one SHA can install different versions.
  - Unit Tests failed in 18 of the 31 failed runs in the window; 13 at "Run unit tests", 5 at "Install dependencies" (resolver conflicts on Dependabot branches).
- **Failure scenario**: an upstream minor release changes behaviour; every open PR goes red at once, or the image deployed from a green SHA differs from what CI tested.
- **Fix**: compile a hashed lock (`uv pip compile --generate-hashes` or `pip-compile`) and install from it in CI and `Dockerfile.prod`; let Dependabot update the lock.
- **Effort**: M

### 10. The main chat is not announced to screen readers, and several controls have no name

- **Severity**: medium
- **Evidence**
  - `grep -n -E 'aria-live|role="log"'` on `frontend/src/components/chat/VirtualizedMessageList.tsx`, `ChatMessageList.tsx` and `ChatSurface.tsx` returns nothing. `aui/AuiMessage.tsx:489,528,648` set `aria-live="off"`. The secondary widget does it right: `chat-widget/ChatMessageList.tsx:65-66` (`role="log" aria-live="polite"`).
  - Icon-only buttons with no accessible name: `frontend/src/components/entities/EntityForm.tsx:382,477`, `RelationshipForm.tsx:184,337`, `EntityMergeTool.tsx:484`. `title` only: `frontend/app/components/Pagination.tsx:70,79,109,118`. `chat/ChatSidebar.tsx:547` announces as a bare number.
  - `frontend/src/components/research-engine/CreateProjectModal.tsx:67` is a hand-rolled overlay with no `role="dialog"`, `aria-modal`, Escape handler, focus trap or focus restore. `dashboard/QuickSearch.tsx:51-60` and `KeyboardShortcuts.tsx:61-73` have the role and Escape but no trap or restore.
  - Destructive clicks with no confirmation or undo: `research-engine/ProjectWorkflow.tsx:379` (remove a project role), `integrations/ConnectedDevices.tsx:264` (revoke consent), `chat/artifact-panel/ArtifactShareDialog.tsx:346` (revoke a share).
  - The `accessibility` Playwright project never runs in CI (item 3).
- **Failure scenario**: a screen-reader user sends a message and hears nothing when the answer arrives; a keyboard user tabs out of the create-project modal into the page behind it.
- **Fix**: a visually hidden polite region that announces on completion (not per token); `aria-label` on the listed buttons; move the three overlays to the existing Radix `ui/dialog.tsx`; `AlertDialog` for the three destructive actions.
- **Effort**: S

## Remaining findings by area

### Frontend: state and correctness

| Finding | Severity | Evidence | Failure scenario | Fix | Effort |
| --- | --- | --- | --- | --- | --- |
| A clean end of stream with no terminal frame is final for the NOUS provider (PLAUSIBLE: how often the backend closes cleanly mid-run is unverified) | medium | `frontend/src/services/agentChatService.ts:844-848` calls `onConnectionLost` only for `codex`; `useChatStreaming.ts:1675-1679` then finishes the run as `error`, and `transportFailureRef` is set only in the `catch` (`:1973-1975`) | A proxy closes the response gracefully; the user sees "This response failed to generate" while the durable run may still finish, with no reattach | Treat an incomplete stream as a transport failure for every provider | S |
| The widget's durable-run poll exits only on `COMPLETED` with output, `FAILED` or `CRASHED` | medium-low | `frontend/src/store/agentChatStore.ts:819,844`; loop is 200 × 3 s (`:743-744`) | A run ends `CANCELED`, `TIMED_OUT`, `EXPIRED` or completes with null output; the composer stays locked for 10 minutes, then reports a timeout | Exit on any terminal status | S |
| Stream ownership is a per-instance ref while the streaming slice is global (PLAUSIBLE) | low-medium | `useChatStreaming.ts:571` `streamOwnerRef`; `:2017-2019` resets the slice after `await reconcileAssistant` (`:1944`) | Leave `/chat` mid-stream and return at once: the old instance's late `finally` wipes the new instance's live bubble and lets a second turn start | Move the owner token to the store, or skip the reset after unmount | M |
| The replay cursor advances on the `id:` line, before the frame is dispatched | low | `frontend/src/services/agentChatService.ts:532-538` | A chunk boundary between `id:` and `data:` followed by a drop skips one frame on resume; terminal reconciliation heals it | Commit the cursor at the frame's blank line | S |
| `ExtractionMatrix` polls every 3 s forever on a permanent error | low | `frontend/src/components/research/ExtractionMatrix.tsx:143-148` (`catch { // keep polling }`) | A missing task id or a 403 polls indefinitely with no error shown | Cap attempts and surface the failure | S |
| A failed document removal is logged only | low | `frontend/app/(dashboard)/projects/[id]/page.tsx:465-471` | The click appears to do nothing | Toast on failure | S |
| `terminal/` has its own SSE parser with no cursor or resume, and appends `rag_context` snapshots (PLAUSIBLE) | low | `frontend/cli/stream.ts`; `terminal/src/adapter.ts:136-138` appends where the web replaces (`useChatStreaming.ts:1527-1528`) | Duplicate citations in the terminal if the backend sends more than one snapshot per turn | Replace instead of append; share one parser | S |

Checked and sound: the `refreshMessages` generation guard and `expectationMet` (`store/chat/slices/messageSlice.ts:189-382`); `loadOlderMessages` tokens (`:436-447`); request tokens on thread and conversation list loads; rename, delete and feedback are not optimistic; abort is not treated as an error and unmount aborts the fetch; persisted chat state holds ids only and is owner-stamped; every `refetchInterval` found stops on a terminal state or pauses in a hidden tab. Query keys are mostly not user-scoped (about 4 of 95 mention a user or org), which is safe today only because `clearUserScopedClientState` clears the cache on account change and the frontend has no organisation switch without sign-out. `monitoringStore`, the analytics and realtime stores and the graph stores are not reset there; they have few or no production importers.

### Frontend: dependencies

| Finding | Severity | Evidence | Fix | Effort |
| --- | --- | --- | --- | --- |
| The `@assistant-ui/react` upgrade surface is small: 5 production files, with two deprecated APIs in use | medium | Installed 0.14.29. `useThread` at `frontend/src/components/chat/aui/AuiMessage.tsx:1134,1135,1145,1150` (deprecated in the installed `.d.ts`: "Use useAuiState"); `makeAssistantToolUI` ×3 in `aui/toolUIs.tsx` and ×1 in `HitlApprovalToolUI.tsx`. `AuiMessage.tsx:1025-1030` matches an upstream error string. What 0.15 removes is UNVERIFIED | Migrate the 8 call sites and 5 test mocks first, then bump in the dedicated `assistant-ui` Dependabot group | M |
| 14 declared packages are never imported | medium | Import grep over `frontend scripts packages terminal tests` finds nothing for `vis-network`, `highlight.js`, `shiki`, `lodash-es`, `json-stringify-pretty-compact`, `@uidotdev/usehooks`, `@use-gesture/react`, `react-hook-form`, `@hookform/resolvers`, `@tanstack/react-query-devtools`, `radix-ui`, `msw`, `@types/d3`, `@types/lodash-es` | Remove them | S |
| Cytoscape (3 packages) and `react-router-dom` serve only dead code | medium | `cytoscape*` imported only at `frontend/src/components/citations/CitationGraph.tsx:25-27`, which has no runtime importer. `react-router-dom` is imported by 13 files: 10 unrouted files under `src/page-components/` and `layout/{Sidebar,Header,AppLayout}.tsx` | Delete the dead trees, then the packages | M |
| `frontend/vercel.json` installs with `--no-frozen-lockfile` | medium | `frontend/vercel.json` `installCommand`; root `vercel.json` uses `--frozen-lockfile`. Which file the Vercel project reads is UNVERIFIED | Make it frozen or delete the duplicate | S |
| `@heroicons/react` is used by 14 files against 231 for `lucide-react` | low | `grep -rl` counts | Port the 14 and drop the package | S |

### Frontend: performance

| Finding | Severity | Evidence | Fix | Effort |
| --- | --- | --- | --- | --- |
| 33 of 44 `page.tsx` files are client components | medium | `'use client'` in 45 of 94 `app/` files and 264 of 604 `src/` files; largest client pages `projects/[id]/page.tsx` (1442 lines), `entities/page.tsx` (1264), `documents/[id]/page.tsx` (784) | Split server data and shell from interactive islands, starting with the two largest | L |
| `framer-motion` is imported statically in 33 production files, including the chat path and auth pages | medium | `chat/VirtualizedMessageList.tsx:13`, `ChatSurface.tsx:10`, `chat/shared/ChatBubble.tsx:13`, `app/(auth)/reset-password/page.tsx:5`; no `LazyMotion` anywhere. Bundle cost UNVERIFIED (no build) | `LazyMotion` with `m`, or CSS for simple fades | M |
| No bundle budget | low | `scripts/analyze-bundle.cjs:16-19` exits 0 without a token and never fails; `next.config.js:40-46` `optimizePackageImports` lists only Next defaults plus two packages that are not installed | Add a size gate to `lint-frontend` | S |
| 7 selector-less store subscriptions on live routes | low | `frontend/src/hooks/useAuth.tsx:55` (37 callers), `hooks/chat/useChatSession.ts:185`, `hooks/useChatPersistence.ts:304`, `app/(dashboard)/projects/[id]/page.tsx:117,139`, `contexts/sidebar-context.tsx:69`. `useChatStore` itself has none | Add selectors | S |
| `setInterval` polls ignore tab visibility | low | `hooks/useDocuments.ts:745` (5 s, capped at 10 min), `entities/EntityMergeTool.tsx:70` and `DocumentEntityExtractor.tsx:63` (2 s); `hooks/useBackendCapabilities.ts:14` refetches every 30 s with no end | Gate on `document.visibilityState` or move to Query | S |

Checked and fine: `pdfjs-dist`, `react-syntax-highlighter` and KaTeX are lazy (`PdfDocumentViewer.tsx:21,143`, `ChatMarkdown.tsx:10,151`); `recharts` reaches only `/analytics`; chat lists, the documents page and the citation list are virtualised or paginated.

### Frontend: quality ratchets

| Finding | Severity | Evidence | Fix | Effort |
| --- | --- | --- | --- | --- |
| The 15 type-check exclusions hide 27 errors in 9 files, and the excluded code is dead | medium | `tsc --noEmit -p tsconfig.json`: exit 0. With the production exclusions removed (temporary config outside the repo): exit 2, 27 errors: `src/stores/graphStore.ts` 11, `components/layout/Header.tsx` 4, `services/monitoringWebsocketService.ts` 3, `monitoring/PerformanceMetricsDashboard.tsx` 3, `monitoring/SystemOverviewDashboard.tsx` 2, `utils/analytics/**` 2, `ui/shadcn-io/**` 1, `ui/enhanced-new-chat-dialog.tsx` 1. Seven excluded paths have 0 errors | Drop the 7 clean exclusions now; delete the dead files instead of fixing them | S-M |
| The coverage floor is measured on a subset | medium | `frontend/vitest.config.mts:48-60` includes only `src/{components,hooks,services,store,utils}`. Outside the denominator: `app/` (94 files), `src/stores/` (11 files including `authStore.ts`), `src/lib/` (23), `src/contexts`, `src/page-components` | Add them to `include` and re-baseline | S |
| The ESLint ratchet tracks totals only | medium | `frontend/quality-baseline.json`: 88 errors, 1808 warnings. `no-explicit-any` is a warning (`frontend/eslint.config.mjs:26`); `as unknown as` (38) and `eslint-disable` (28, of which 15 are `react-hooks/set-state-in-effect`) are untracked | Add per-rule counts for `any`, `as unknown as` and disables | S |
| Eight component directories have no test | medium | `components/monitoring` (6 files, 2909 lines), `analytics` (6, 2322), `notifications` (9, 1706), `arxiv/tabs` (4, 1301), `research/steps` (5, 759), `dashboard` (5, 611), `llm-chat` (6, 561), `elements` (5, 329) | Test or delete (several are dead) | M |
| Vitest retries twice in CI | low | `frontend/vitest.config.mts:41` | Report retried tests, as `agent-eval.yml` now does | S |

Error boundaries beyond the missing route-level `error.tsx` were not completed: `global-error.tsx` (401 bytes) and three component boundaries (`CitationErrorBoundary`, `EntityErrorBoundary`, `ResearchErrorBoundary`) exist; their mounting is UNVERIFIED.

### CI and supply chain

| Finding | Severity | Evidence | Fix | Effort |
| --- | --- | --- | --- | --- |
| The weekly agent eval failed 11 of its last 12 scheduled runs | medium | `gh api .../workflows/agent-eval.yml/runs`: one success (2026-09-28) since 2026-08-24. The `--reruns 1` change (`agent-eval.yml:137`) has not run on schedule yet; next run 2026-11-16 | See 2026-11-13 item 7; check the 11-16 run before changing more | S |
| `agent-eval.yml` reads two secrets that do not exist | low | `agent-eval.yml:50-51` reference `AZURE_OPENAI_EMBEDDING_ENDPOINT` and `AZURE_OPENAI_EMBEDDING_API_KEY`; neither is in `gh api .../actions/secrets` (36 names) | Add them or remove the lines | S |
| 16 repository secrets are referenced by no workflow | medium | Present but unused: `CLAUDE_APP_ID`, `CLAUDE_APP_PRIVATE_KEY`, `DEPOT_PROJECT_ID`, `DEPOT_TOKEN`, `DO_REGISTRY_TOKEN`, `DO_SPACES_ACCESS_KEY`, `DO_SPACES_SECRET_KEY`, `EVAL_QDRANT_URL`, `EVAL_REDIS_URL`, `NEO4J_AUTH`, `NEO4J_AUTH_PROD`, `AWS_ROLE_TO_ASSUME`, `NEXT_PUBLIC_SENTRY_DSN`, `NEXT_PUBLIC_SUPABASE_ANON_KEY`, `NEXT_PUBLIC_SUPABASE_URL`, `SENTRY_AUTH_TOKEN` (`grep -o 'secrets\.[A-Z0-9_]*' -r .github`) | Delete them, and revoke the DigitalOcean and GitHub App credentials at the source | S |
| The self-hosted runner is reachable only from `schedule` and `workflow_dispatch` | info | `agent-eval.yml:8-10,22` is the only `self-hosted` job; no workflow uses `pull_request_target`; fork approval is `all_external_contributors`; runner `do-droplet-runner` is online | Unchanged from 2026-11-13: move the eval to a hosted runner now that Redis is a service container | S |
| No script injection found | info | Every `${{ github.event.* }}`, `github.head_ref` and `inputs.*` use is passed through `env:` (`test-pipeline.yml:65,138`, `docker-build.yml:53`, `agent-eval.yml:44`); `opencode.yml:15-25` gates on phrase and `author_association`; all `uses:` are SHA-pinned; AWS OIDC trust is limited to `refs/heads/develop` (`infrastructure/terraform/github-oidc.tf:47`) | None | - |
| Performance Tests can never run | low | `test-pipeline.yml:1626`: `if: github.event_name == 'push' && github.ref == 'refs/heads/main'`; `gh api .../branches/main/protection` returns `Branch not found`. The job also has no `timeout-minutes` | Delete the job or point it at `develop` on a schedule | S |
| `trigger-deploy.yml` uses Node 20 against the Node 24 contract and triggers on the missing `main` | low | `trigger-deploy.yml:5,24`; zero runs in `gh api .../workflows/trigger-deploy.yml/runs` | Fix with the Trigger deploy-branch decision from 2026-11-13 item 9 | S |
| Supabase Migrations last ran on 2026-09-29 and failed | low | Run [36624004236](https://github.com/goodwiins/NOUS-Research-Assistant/actions/runs/36624004236). Cause UNVERIFIED (log not read) | Read the log; confirm migrations since then were applied | S |
| Unit Tests skip 271 tests and carry 3 stale `xfail` markers | low | Unit log of run [43105860123](https://github.com/goodwiins/NOUS-Research-Assistant/actions/runs/43105860123): `271 skipped, 3 xpassed`; 180 skips are "`ORCHESTRATION_TEST_DATABASE_URL` is not configured" (those suites do run in the integration job, `test-pipeline.yml:1035-1040`), 55 are "Local PostgreSQL unreachable at localhost:54322"; XPASS in `backend/tests/unit/services/test_llm_providers.py::TestOllamaProvider` | Remove the 3 `xfail` markers; deselect the PostgreSQL suites from the unit job instead of skipping; find a CI home for the 55 | S |
| Two workflow registrations point at deleted files | low | `gh api .../actions/workflows` lists `E2E Tests` (`.github/workflows/e2e-tests.yml`) as `active`; the file is not on `develop` | Disable it in the Actions UI | S |
| Actions cache is at the 10 GB limit | low | `gh api .../actions/cache/usage`: 10,091,845,095 bytes in 114 caches | Expect evictions; drop the broad `restore-keys` fallbacks | S |
| Pin comments disagree for one SHA | low | `actions/checkout@3d3c42e…` is labelled `# v4` (`agent-eval.yml:63`, `trigger-deploy.yml:18`) and `# v7.0.1` (`opencode.yml:37`) | Correct the comments | S |

CI health in the window (200 runs, 2026-10-29 to 2026-11-14): pull-request runs were 95 success, 30 failure, 10 cancelled. Failed jobs across the 31 failed runs: Unit Tests 18, Lint Backend 6, OpenAPI Contract Ratchet 6, Golden Replay 5, Alembic Migration Check 5, Lint Frontend 3, Integration Tests 3, Frontend Tests 1. Six runs were re-run; three went green on a later attempt after a failed `CI Plan` (2026-11-09, during the Actions billing lock), a cancelled Resilience job and one E2E failure. No test that is flaky today was identified: the two repeat offenders (`test_old_receipt_cannot_approve_next_interrupt_in_same_node`, `test_retry_exponential_backoff`) have since been fixed. Successful `develop` push runs took 39 to 50 minutes before 2026-11-13 16:00 UTC and 21 and 19 minutes in the two since.

Open Dependabot PRs on 2026-11-14: #2107 and #2111 fail at `pip install` on the OpenTelemetry instrumentation mismatch (#2126 addresses it). #2095, #2109, #2112, #2116 and #2117 have no failing check (18 success, 1 neutral, 1 skipped each) and `mergeable_state: clean`; they wait on a human decision about major versions, not on CI. #2083 was closed unmerged on 2026-11-12.

## Corrections to the 2026-11-13 audit

- **Item 8, "Dependabot PRs that cannot resolve"**: only #2107 and #2111 fail today. #2095 (Python 3.15 base image), #2109 (pip majors) and #2112 (transformers 5) are green and mergeable; note that #2095 edits `backend/docker/` files that default to `python:3.14-slim`, while the deployed image is built with `PYTHON_VERSION=3.11` (`docker-build.yml:114`) and CI tests on 3.11 (`test-pipeline.yml:16`). #2083 is closed.
- **Item 8, alert count**: 53 open, not 42 (item 6).
- **Item 6, branch protection**: also allows force pushes (item 1).
- **Frontend table, "15 production paths excluded, including `AppLayout.tsx`, `Header.tsx`, `Sidebar.tsx`"**: those three are imported only by each other and by tests; the live shell is `SidebarLayout`/`AppSidebar`. The exclusions cover dead code, so the fix is deletion, not "earn them back, layout first".
- **Frontend table, `ignoreBuildErrors`**: `frontend/next.config.js:32` is `process.env.NODE_ENV !== 'production'`; `next build` defaults `NODE_ENV` to production and `frontend/Dockerfile.prod:56` sets it, so production builds are strict. Low priority.
- **Frontend table, icon buttons**: 5 unnamed buttons confirmed by reading the JSX, all on the entities route (item 10), replacing the "4 of 35, UNVERIFIED" heuristic.
- **Frontend table, counts**: 193 `: any` and 38 `as unknown as` over 698 production files (the 186 and 33 used a narrower file set).
- **Tests and CI table, critical path**: now three samples after #2104 rather than one; 21 and 19 minutes for the two most recent.
