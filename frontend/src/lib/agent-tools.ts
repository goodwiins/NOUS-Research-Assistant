/**
 * Agent tools whose successful completion changes project data, so the
 * project rail and project pages must refetch.
 *
 * Single source for both chat surfaces, `/chat` (useChatStreaming) and the
 * agent widget (agentChatStore). Two hand-kept copies drifted before
 * (GOO-651): one listed `ingest_arxiv`, which the backend never emits (the
 * tool is `ingest_arxiv_papers`), and the other missed `create_project`.
 *
 * Every name must be a tool the backend declares (`"name": "<tool>"` in
 * backend/src/services/agent/tools_impl.py); agent-tools.test.ts checks it.
 */
export const PROJECT_MUTATING_TOOLS: ReadonlySet<string> = new Set([
  'add_document_to_project',
  'create_draft',
  'create_project',
  'create_project_note',
  'ingest_arxiv_papers',
  'revise_draft',
]);
