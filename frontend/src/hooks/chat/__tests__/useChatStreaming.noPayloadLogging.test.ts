/**
 * GOO-638 (FL-4): Sentry's consoleLoggingIntegration forwards console.log,
 * console.warn and console.error whenever a DSN is set, so anything the chat
 * stream hook prints about a tool call leaves the browser. Tool arguments,
 * tool results and approval payloads carry user data; they must never reach
 * the console.
 */
import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

const SOURCE = readFileSync(
  resolve(__dirname, '../useChatStreaming.ts'),
  'utf8'
);

/** Every console call with its argument list, across line breaks. */
function consoleCalls(source: string): string[] {
  const calls: string[] = [];
  const pattern = /console\.(?:log|info|debug|warn|error)\s*\(/g;
  let match: RegExpExecArray | null;
  while ((match = pattern.exec(source))) {
    let depth = 1;
    let i = pattern.lastIndex;
    while (i < source.length && depth > 0) {
      if (source[i] === '(') depth += 1;
      else if (source[i] === ')') depth -= 1;
      i += 1;
    }
    calls.push(source.slice(match.index, i));
  }
  return calls;
}

describe('useChatStreaming console hygiene', () => {
  it('finds the console calls it is checking', () => {
    expect(consoleCalls(SOURCE).length).toBeGreaterThan(0);
  });

  it.each(['args', 'result', 'confirmation', 'dynamicTitle', 'content'])(
    'never passes `%s` to the console',
    (identifier) => {
      const offending = consoleCalls(SOURCE).filter((call) =>
        new RegExp(`[,(]\\s*${identifier}\\b`).test(call)
      );
      expect(offending).toEqual([]);
    }
  );
});
