/**
 * GOO-651 (FL-1) / GOO-727: the project-mutating tool list must equal the
 * backend's mutating-tool set minus a reviewed allowlist, and both chat
 * surfaces must use the one shared list. The backend mirror of this check is
 * backend/tests/unit/services/agent/test_frontend_mutating_tool_contract.py,
 * so a backend-only PR that adds or renames a mutating tool fails there.
 */
import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import {
  MUTATING_TOOLS_WITHOUT_PROJECT_DATA,
  PROJECT_MUTATING_TOOLS,
} from '../agent-tools';

const REPO = resolve(__dirname, '../../../..');
const read = (path: string): string =>
  readFileSync(resolve(REPO, path), 'utf8');

/** Tool names the backend declares to the model. */
function backendToolNames(): Set<string> {
  const source = read('backend/src/services/agent/tools_impl.py');
  return new Set(
    Array.from(source.matchAll(/"name":\s*"([a-z_]+)"/g), (m) => m[1])
  );
}

/** `_MUTATING_TOOLS = frozenset({...})` in the backend identity ledger. */
function backendMutatingTools(source: string): Set<string> {
  const block = /_MUTATING_TOOLS\s*=\s*frozenset\(\s*\{([^}]*)\}\s*\)/.exec(
    source
  );
  if (!block) throw new Error('_MUTATING_TOOLS not found in identity_ledger.py');
  return new Set(Array.from(block[1].matchAll(/"([a-z_]+)"/g), (m) => m[1]));
}

const LEDGER = 'backend/src/services/agent/identity_ledger.py';

describe('PROJECT_MUTATING_TOOLS', () => {
  it('equals the backend mutating tools minus the reviewed allowlist', () => {
    const backend = backendMutatingTools(read(LEDGER));
    expect(backend.size).toBeGreaterThan(5);
    const expected = [...backend]
      .filter((tool) => !(tool in MUTATING_TOOLS_WITHOUT_PROJECT_DATA))
      .sort();
    expect([...PROJECT_MUTATING_TOOLS].sort()).toEqual(expected);
  });

  it('only allowlists real backend mutating tools, each with a reason', () => {
    const backend = backendMutatingTools(read(LEDGER));
    for (const [tool, reason] of Object.entries(
      MUTATING_TOOLS_WITHOUT_PROJECT_DATA
    )) {
      expect(backend).toContain(tool);
      expect(PROJECT_MUTATING_TOOLS.has(tool)).toBe(false);
      expect(reason.trim().length).toBeGreaterThan(20);
    }
  });

  it('parses a new tool out of the ledger block', () => {
    expect(
      backendMutatingTools(
        '_MUTATING_TOOLS = frozenset(\n    {\n        "a_tool",\n        "b_tool",\n    }\n)'
      )
    ).toEqual(new Set(['a_tool', 'b_tool']));
  });

  it('reads a non-trivial backend tool registry', () => {
    expect(backendToolNames().size).toBeGreaterThan(10);
  });

  it.each([...PROJECT_MUTATING_TOOLS])(
    '%s is a tool the backend declares',
    (tool) => {
      expect(backendToolNames()).toContain(tool);
    }
  );

  it.each(['create_project', 'ingest_arxiv_papers'])(
    'includes %s, which both surfaces must refetch after',
    (tool) => {
      expect(PROJECT_MUTATING_TOOLS.has(tool)).toBe(true);
    }
  );

  it.each([
    'frontend/src/hooks/chat/useChatStreaming.ts',
    'frontend/src/store/agentChatStore.ts',
  ])('%s uses the shared list instead of its own copy', (path) => {
    const source = read(path);
    expect(source).toMatch(
      /import \{ PROJECT_MUTATING_TOOLS \} from '@\/lib\/agent-tools';/
    );
    expect(source).not.toMatch(/const PROJECT_MUTATING_TOOLS\s*=/);
  });
});
