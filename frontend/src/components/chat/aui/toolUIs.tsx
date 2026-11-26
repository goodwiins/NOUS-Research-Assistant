'use client';

import type { ReactElement } from 'react';
import {
  AuiConfig,
  Tools,
  type ToolCallMessagePartProps,
  type Toolkit,
} from '@assistant-ui/react';
import { FileSearch } from 'lucide-react';

import { HitlApprovalRenderer, HITL_APPROVAL_TOOL } from './HitlApprovalToolUI';

/**
 * Declarative per-tool renderers (P2). Each is registered by tool name as a
 * render-only `backend` toolkit entry; a matching tool-call part then routes
 * to it instead of the generic `ToolFallback`. Renderers branch on
 * `status.type` (running → skeleton, incomplete → error, complete → result)
 * and read the structured (backend-redacted) `args` threaded through
 * `ActivityStep.args`.
 *
 * Unregistered tools keep the strong generic `ToolFallback`, so this registry
 * is override-only — add a card here when a tool deserves bespoke rendering.
 */

function resultCount(result: unknown): number | undefined {
  if (typeof result === 'string') {
    try {
      return resultCount(JSON.parse(result));
    } catch {
      return undefined;
    }
  }
  if (result && typeof result === 'object') {
    const r = result as Record<string, unknown>;
    for (const key of ['count', 'total', 'num_results']) {
      if (typeof r[key] === 'number') return r[key] as number;
    }
    for (const key of ['documents', 'results', 'items']) {
      if (Array.isArray(r[key])) return (r[key] as unknown[]).length;
    }
  }
  return undefined;
}

/** Bespoke document-search card, shared by runtime and live store rendering. */
export function SearchDocumentsToolRenderer({
  args,
  status,
  result,
  isError,
}: ToolCallMessagePartProps<{ query?: string }, unknown>): ReactElement {
  const query = args?.query;
  const running = status.type === 'running';
  const failed = isError || status.type === 'incomplete';
  const hits = resultCount(result);
  const statusLabel = running
    ? 'Searching documents'
    : failed
      ? 'Document search failed'
      : 'Searched documents';
  return (
    <div
      data-slot="search-documents-tool"
      className="my-1.5 flex min-w-0 items-center gap-2 rounded-lg border border-(--nous-border-1) bg-(--nous-bg-2)/60 px-2.5 py-1.5 text-xs text-(--nous-fg-2)"
    >
      <FileSearch
        className="h-3.5 w-3.5 shrink-0 text-(--nous-fg-3)"
        strokeWidth={1.8}
        aria-hidden
      />
      <span className="shrink-0 text-(--nous-fg-3)">{statusLabel}</span>
      {query ? (
        <span
          className="min-w-0 truncate rounded bg-(--nous-sol-subtle) px-1.5 py-0.5 text-(--nous-fg-accent)"
          style={{ fontFamily: 'var(--nous-font-mono)' }}
        >
          {query}
        </span>
      ) : null}
      <span className="ml-auto shrink-0 text-(--nous-fg-3)">
        {running
          ? 'searching…'
          : failed
            ? 'failed'
            : hits !== undefined
              ? `${hits} ${hits === 1 ? 'result' : 'results'}`
              : 'done'}
      </span>
    </div>
  );
}

/** Render-only toolkit for backend-owned tools: `type: 'backend'` entries carry
 * no description, parameters or executor, so model context gets inert
 * `{ type: 'backend' }` entries and the runtime never executes them. */
const NOUS_TOOLKIT = {
  search_documents: {
    type: 'backend',
    render: SearchDocumentsToolRenderer,
  },
  [HITL_APPROVAL_TOOL]: {
    type: 'backend',
    display: 'standalone',
    render: HitlApprovalRenderer,
  },
} satisfies Toolkit;

/** Registers the web renderers for backend-owned tools on the runtime's
 * `tools` scope (pass as `AssistantRuntimeProvider`'s `config`). */
export const NOUS_TOOLS_CONFIG = AuiConfig({
  tools: Tools({ toolkit: NOUS_TOOLKIT }),
});
