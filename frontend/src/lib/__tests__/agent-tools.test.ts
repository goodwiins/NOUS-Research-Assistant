/**
 * GOO-651 (FL-1): the project-mutating tool list must name real backend
 * tools, and both chat surfaces must use the one shared list.
 */
import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { PROJECT_MUTATING_TOOLS } from '../agent-tools';

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

describe('PROJECT_MUTATING_TOOLS', () => {
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
