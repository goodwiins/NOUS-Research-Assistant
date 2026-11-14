import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { ChatCompletionAnnouncer } from '@/components/chat/ChatCompletionAnnouncer';

type Msg = { role: 'user' | 'assistant'; content: string };

const user: Msg = { role: 'user', content: 'What is attention?' };
const answer: Msg = {
  role: 'assistant',
  content:
    'Attention lets a model weigh tokens against each other. It is used in transformers.',
};

function region(): HTMLElement {
  return screen.getByTestId('chat-completion-announcer');
}

describe('ChatCompletionAnnouncer', () => {
  it('is a polite visually hidden live region that starts empty', () => {
    render(
      <ChatCompletionAnnouncer isStreaming={false} threadId="t1" messages={[user]} />
    );
    expect(region()).toHaveAttribute('aria-live', 'polite');
    expect(region()).toHaveClass('sr-only');
    expect(region()).toHaveTextContent('');
  });

  it('stays empty while streaming, even as content grows', () => {
    const { rerender } = render(
      <ChatCompletionAnnouncer isStreaming={false} threadId="t1" messages={[user]} />
    );
    rerender(
      <ChatCompletionAnnouncer isStreaming threadId="t1" messages={[user]} />
    );
    rerender(
      <ChatCompletionAnnouncer
        isStreaming
        threadId="t1"
        messages={[user, { role: 'assistant', content: 'Attention lets' }]}
      />
    );
    expect(region()).toHaveTextContent('');
  });

  it('announces once on completion with the first sentence', () => {
    const { rerender } = render(
      <ChatCompletionAnnouncer isStreaming threadId="t1" messages={[user, answer]} />
    );
    expect(region()).toHaveTextContent('');
    rerender(
      <ChatCompletionAnnouncer isStreaming={false} threadId="t1" messages={[user, answer]} />
    );
    expect(region().textContent).toBe(
      'Answer ready. Attention lets a model weigh tokens against each other.'
    );
    // Later re-renders (e.g. reconciliation) do not re-announce or change it.
    const before = region().textContent;
    rerender(
      <ChatCompletionAnnouncer
        isStreaming={false}
        threadId="t1"
        messages={[user, { ...answer, content: answer.content + ' More.' }]}
      />
    );
    expect(region().textContent).toBe(before);
  });

  it('does not announce when switching threads without a streamed turn', () => {
    const { rerender } = render(
      <ChatCompletionAnnouncer isStreaming={false} threadId="t1" messages={[user]} />
    );
    rerender(
      <ChatCompletionAnnouncer isStreaming={false} threadId="t2" messages={[user, answer]} />
    );
    expect(region()).toHaveTextContent('');
  });
});
