import { render, screen, type RenderResult } from '@testing-library/react';
import type { ComponentProps, ReactElement } from 'react';
import { describe, expect, it } from 'vitest';

import { ChatCompletionAnnouncer } from '@/components/chat/ChatCompletionAnnouncer';

type Props = ComponentProps<typeof ChatCompletionAnnouncer>;
type Msg = Props['messages'][number];

const user: Msg = { role: 'user', content: 'What is attention?' };
const answer: Msg = {
  role: 'assistant',
  content: '1. Attention lets a model weigh tokens. See [Doc 1].',
};

function region(): HTMLElement {
  return screen.getByTestId('chat-completion-announcer');
}

function ui(
  isStreaming: boolean,
  messages: Msg[],
  threadId = 't1'
): ReactElement {
  return (
    <ChatCompletionAnnouncer
      isStreaming={isStreaming}
      threadId={threadId}
      messages={messages}
    />
  );
}

function view(isStreaming: boolean, messages: Msg[]): RenderResult {
  return render(ui(isStreaming, messages));
}

describe('ChatCompletionAnnouncer', () => {
  it('is a polite visually hidden live region that starts empty', () => {
    view(false, [user]);
    expect(region()).toHaveAttribute('aria-live', 'polite');
    expect(region()).toHaveClass('sr-only');
    expect(region()).toHaveTextContent('');
  });

  it('stays empty while streaming, even as content grows', () => {
    const { rerender } = view(false, [user]);
    rerender(ui(true, [user]));
    rerender(ui(true, [user, { role: 'assistant', content: 'Attention lets' }]));
    expect(region()).toHaveTextContent('');
  });

  it('announces only "Answer ready." on completion, never answer text', () => {
    const { rerender } = view(true, [user, answer]);
    expect(region()).toHaveTextContent('');
    rerender(ui(false, [user, answer]));
    expect(region().textContent).toBe('Answer ready.');
  });

  it('announces "Response stopped." for a stopped turn', () => {
    const { rerender } = view(true, [user]);
    rerender(
      ui(false, [
        user,
        { role: 'assistant', content: 'Partial', metadata: { stopped: true } },
      ])
    );
    expect(region().textContent).toBe('Response stopped.');
  });

  it('stays silent for an errored turn and does not announce later', () => {
    const { rerender } = view(true, [user]);
    rerender(
      ui(false, [
        user,
        { role: 'assistant', content: '', error: { message: 'boom' } },
      ])
    );
    expect(region()).toHaveTextContent('');
    // The pending flag was cleared: a later normal message must not announce.
    rerender(ui(false, [user, answer]));
    expect(region()).toHaveTextContent('');
  });

  it('stays silent while the turn is paused for a confirmation', () => {
    const { rerender } = view(true, [user]);
    rerender(
      ui(false, [
        user,
        {
          role: 'assistant',
          content: 'Need approval',
          pendingApproval: { id: 'a', tools: [] },
        },
      ])
    );
    expect(region()).toHaveTextContent('');
    rerender(ui(false, [user, answer]));
    expect(region()).toHaveTextContent('');
  });

  it('waits for content when the assistant message is still empty', () => {
    const { rerender } = view(true, [user]);
    rerender(ui(false, [user, { role: 'assistant', content: '' }]));
    expect(region()).toHaveTextContent('');
    rerender(ui(false, [user, answer]));
    expect(region().textContent).toBe('Answer ready.');
  });

  it('announces once: reconciliation re-renders never announce again', () => {
    const { rerender } = view(true, [user, answer]);
    rerender(ui(false, [user, answer]));
    expect(region().textContent).toBe('Answer ready.');

    const mutations: MutationRecord[] = [];
    const observer = new MutationObserver((records) => mutations.push(...records));
    observer.observe(region(), {
      childList: true,
      characterData: true,
      subtree: true,
    });

    // Terminal reconciliation swaps in the persisted row: new id, new
    // content. If the pending flag lingered, this one (flagged stopped, so
    // it would read differently) would be announced as a second turn.
    rerender(
      ui(false, [
        user,
        {
          ...answer,
          id: 'db-123',
          content: answer.content + ' Persisted.',
          metadata: { stopped: true },
        } as Msg,
      ])
    );
    observer.disconnect();
    expect(region().textContent).toBe('Answer ready.');
    expect(mutations).toHaveLength(0);
  });

  it('does not announce when switching threads without a streamed turn', () => {
    const { rerender } = view(false, [user]);
    rerender(ui(false, [user, answer], 't2'));
    expect(region()).toHaveTextContent('');
  });
});
