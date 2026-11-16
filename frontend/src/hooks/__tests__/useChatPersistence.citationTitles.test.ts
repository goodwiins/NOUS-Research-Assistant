import { describe, expect, it } from 'vitest';

import { mapDbMessageToUI } from '@/hooks/useChatPersistence';
import { mapDbMessageToChatPageMessage } from '@/components/chat/shared/cloudMessageView';
import { MessageRole, type ChatMessage } from '@/types/workspace';

function persistedMessage(
  citation: NonNullable<ChatMessage['citations']>[number]
): ChatMessage {
  return {
    id: 'message-1',
    thread_id: 'thread-1',
    role: MessageRole.ASSISTANT,
    content: 'Answer [Doc 1]',
    created_at: '2026-11-16T12:00:00Z',
    updated_at: '2026-11-16T12:00:00Z',
    citations: [citation],
    attachments: [],
  } as ChatMessage;
}

describe('persisted citation title extraction', () => {
  const adversarialSnippet =
    'arXiv:2401.12345 - Smith ' + 'A  '.repeat(26) + 'Z';

  it('keeps a shared-thread citation read responsive for failing author-like text', () => {
    const message = persistedMessage({
      id: 'citation-1',
      snippet: adversarialSnippet,
    });

    const started = performance.now();
    const citation = mapDbMessageToUI(message).citations?.[0];
    const elapsedMs = performance.now() - started;

    expect(citation?.title).toMatch(/^Smith A/);
    expect(citation?.content).toBe(adversarialSnippet);
    expect(elapsedMs).toBeLessThan(500);
  });

  it('uses a stored document title without parsing an adversarial snippet', () => {
    const message = persistedMessage({
      id: 'citation-1',
      document_title: 'Stored paper title',
      snippet: adversarialSnippet,
    });

    const started = performance.now();
    const citation = mapDbMessageToUI(message).citations?.[0];
    const elapsedMs = performance.now() - started;

    expect(citation?.title).toBe('Stored paper title');
    expect(elapsedMs).toBeLessThan(500);
  });

  const structuredWhitespaceSnippet =
    'Title:' + ' '.repeat(1500) + 'Paper\nBody';

  it('keeps the legacy persisted mapper responsive for a failing structured title pattern', () => {
    const message = persistedMessage({
      id: 'citation-1',
      snippet: structuredWhitespaceSnippet,
    });

    const started = performance.now();
    const citation = mapDbMessageToUI(message).citations?.[0];
    const elapsedMs = performance.now() - started;

    expect(citation?.title).toBe('Paper');
    expect(elapsedMs).toBeLessThan(500);
  });

  it('keeps the canonical persisted mapper responsive for the same structured title', () => {
    const message = persistedMessage({
      id: 'citation-1',
      snippet: structuredWhitespaceSnippet,
    });

    const started = performance.now();
    const citation = mapDbMessageToChatPageMessage(message).citations?.[0];
    const elapsedMs = performance.now() - started;

    expect(citation?.title).toBe('Paper');
    expect(elapsedMs).toBeLessThan(500);
  });

  it('falls back from a blank stored title to a legacy snippet title', () => {
    const citation = mapDbMessageToUI(
      persistedMessage({
        id: 'citation-1',
        document_title: '   ',
        snippet: 'Title: A useful research paper Authors: Jane Doe',
      })
    ).citations?.[0];

    expect(citation?.title).toBe('A useful research paper');
  });

  it('reads a short title from a large snippet and retains the full content', () => {
    const snippet =
      'Title: A useful research paper Authors: Jane Doe\n' +
      'Body text. '.repeat(100_000);
    const citation = mapDbMessageToUI(
      persistedMessage({ id: 'citation-1', snippet })
    ).citations?.[0];

    expect(citation?.title).toBe('A useful research paper');
    expect(citation?.content).toBe(snippet);
  });

  it('uses the first content line for a plain legacy citation', () => {
    const citation = mapDbMessageToUI(
      persistedMessage({
        id: 'citation-1',
        snippet: 'Authors: Jane Doe\nA useful research paper\nAbstract: Body',
      })
    ).citations?.[0];

    expect(citation?.title).toBe('A useful research paper');
  });
});
