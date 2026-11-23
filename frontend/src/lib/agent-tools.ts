/**
 * Agent tools whose successful completion changes project data, so the
 * project rail and project pages must refetch.
 *
 * Single source for both chat surfaces, `/chat` (useChatStreaming) and the
 * agent widget (agentChatStore). Two hand-kept copies drifted before
 * (GOO-651): one listed `ingest_arxiv`, which the backend never emits (the
 * tool is `ingest_arxiv_papers`), and the other missed `create_project`.
 *
 * Contract (GOO-727): this set must EQUAL the backend's mutating-tool set
 * (`_MUTATING_TOOLS` in backend/src/services/agent/identity_ledger.py) minus
 * MUTATING_TOOLS_WITHOUT_PROJECT_DATA below. Both sides enforce it, so a
 * frontend-only or a backend-only PR that breaks it fails its own CI:
 *  - frontend/src/lib/__tests__/agent-tools.test.ts
 *  - backend/tests/unit/services/agent/test_frontend_mutating_tool_contract.py
 */
export const PROJECT_MUTATING_TOOLS: ReadonlySet<string> = new Set([
  'add_document_to_project',
  'create_draft',
  'create_project',
  'create_project_note',
  'ingest_arxiv_papers',
  'revise_draft',
]);

/**
 * Backend mutating tools that deliberately do NOT trigger a project refetch.
 * Every entry needs a reason; adding one is a reviewed decision.
 */
export const MUTATING_TOOLS_WITHOUT_PROJECT_DATA: Readonly<
  Record<string, string>
> = {
  execute_code:
    'Runs code in the per-conversation E2B sandbox; writes no project, document, note or draft rows.',
  forget_memory:
    "Deletes the user's saved memories, which are not shown on project surfaces.",
};
