# GOO-622 Citation Parsing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep persisted citation title extraction responsive for adversarial snippets while preserving useful legacy titles.

**Architecture:** Both persisted-message mappers prefer a stored title and infer legacy titles from a bounded snippet prefix. They share one linear structured-title helper; the legacy mapper also uses a linear author-list check. The full snippet remains available as citation content.

**Tech Stack:** TypeScript, Vitest, React/Next frontend.

**Spec:** [GOO-622](https://linear.app/goodwiinz/issue/GOO-622/security-bound-citation-title-parsing-to-prevent-browser-redos), assessed against `origin/develop` at `3c548349` on 2026-11-16.

## Global Constraints

- Keep the shared checkout untouched; implement in a worktree based on fresh `origin/develop`.
- Use pnpm 10.18.2 and Node 24.
- Preserve legacy arXiv, DOI, and plain-text title extraction.
- Keep the complete citation content in the UI message; bound only title inference.

## Review Focus

- A failing author-like string with repeated whitespace must return promptly.
- A failing `Title:` pattern with repeated whitespace must return promptly in both persisted-message mappers.
- A large snippet with a short title at the start must retain that title.
- A stored document title must bypass snippet parsing.
- A whitespace-only stored title must fall back to the snippet.
- A legacy title without a structured prefix must still use its first content line.

---

### Task 1: Bound and simplify citation title extraction

**Files:**
- Modify: `frontend/src/hooks/useChatPersistence.ts`
- Modify: `frontend/src/utils/citationNormalizer.ts`
- Test: `frontend/src/hooks/__tests__/useChatPersistence.citationTitles.test.ts`

**Interfaces:**
- Consumes: `ChatMessage` from `@/types/workspace`.
- Produces: unchanged `mapDbMessageToUI(dbMsg: ChatMessage): UIMessage`.

- [x] **Step 1: Write failing tests** for the Review Focus inputs through both persisted message mappers, checking title and processing time.
- [x] **Step 2: Run the focused Vitest file** and confirm the adversarial and stored-title cases fail for the reported reasons.
- [x] **Step 3: Replace the overlapping author-list regex and structured-title regex, bound title parsing input, and prefer a usable stored title.**
- [x] **Step 4: Run focused Vitest, the frontend type check, and the repository frontend quality gate.**
- [x] **Step 5: Inspect the patch for another route into the regex and run a legitimate citation control.**
- [ ] **Step 6: Commit and open a PR against `develop` with `Fixes GOO-622` in the body.**
