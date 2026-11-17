import { describe, expect, it } from 'vitest';

import { mapDbMessageToUI } from '@/hooks/useChatPersistence';
import {
  MessageRole,
  type ChatMessage,
  type Citation,
} from '@/types/workspace';

function messageWithCitation(citation: Omit<Citation, 'id'>): ChatMessage {
  return {
    id: 'msg-1',
    thread_id: 'thread-A',
    role: MessageRole.ASSISTANT,
    content: 'answer',
    created_at: '2026-11-16T00:00:00Z',
    citations: [{ id: 'cit-1', ...citation }],
    attachments: [],
  } as ChatMessage;
}

/**
 * A failing "author-like" line: a capitalised surname followed by many
 * initials separated by multi-space runs, ending without "," or "and".
 * Under the old overlapping `(?:\s+[A-Z]\.?\s*)+` each whitespace run could
 * be split between the trailing `\s*` and the next `\s+`, so matching was
 * exponential in the number of initials (GOO-622): this input took
 * ~200-450 ms with the old pattern (each extra initial multiplies that
 * several-fold), versus well under a millisecond now. It is kept small so a
 * regression fails the time budget instead of hanging the suite.
 */
const PATHOLOGICAL_AUTHOR_LINE = 'Aa' + ' A   '.repeat(12) + '!';
const BUDGET_MS = 100;

describe('mapDbMessageToUI citation titles (GOO-622)', () => {
  it('parses a pathological author-like snippet in linear time', () => {
    const snippet = `arXiv:2401.12345 - ${PATHOLOGICAL_AUTHOR_LINE}`;
    const started = performance.now();
    const ui = mapDbMessageToUI(messageWithCitation({ snippet }));
    const elapsed = performance.now() - started;

    expect(elapsed).toBeLessThan(BUDGET_MS);
    expect(ui.citations?.[0]?.title).toBeTruthy();
    // The full snippet is still exposed as preview content.
    expect(ui.citations?.[0]?.content).toBe(snippet);
  });

  it('bounds a very long snippet before matching', () => {
    const snippet = 'arXiv:2401.12345 - ' + 'Word '.repeat(200_000);
    const started = performance.now();
    const ui = mapDbMessageToUI(messageWithCitation({ snippet }));

    expect(performance.now() - started).toBeLessThan(BUDGET_MS);
    expect(ui.citations?.[0]?.title.length).toBeLessThanOrEqual(150);
  });

  it('prefers document_title and does not parse the snippet', () => {
    const ui = mapDbMessageToUI(
      messageWithCitation({
        document_title: 'Stored Title',
        snippet: `arXiv:2401.12345 - ${PATHOLOGICAL_AUTHOR_LINE}`,
      })
    );
    expect(ui.citations?.[0]?.title).toBe('Stored Title');
  });

  it('still skips real author lists and extracts real titles', () => {
    const authors = mapDbMessageToUI(
      messageWithCitation({
        snippet: 'arXiv:2401.12345 - Smith J. K., Doe A.\nA Real Paper Title',
      })
    );
    expect(authors.citations?.[0]?.title).not.toMatch(/^Smith J\. K\.,/);

    const titled = mapDbMessageToUI(
      messageWithCitation({
        snippet: 'arXiv:2401.12345 - Attention Is All You Need',
      })
    );
    expect(titled.citations?.[0]?.title).toBe('Attention Is All You Need');
  });
});
