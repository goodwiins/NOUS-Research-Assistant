'use client';

import { useState, type ReactElement } from 'react';

interface AnnouncerMessage {
  role: 'user' | 'assistant';
  content: string;
}

interface ChatCompletionAnnouncerProps {
  /** True while the displayed thread's turn is still growing. */
  isStreaming: boolean;
  threadId: string | null;
  messages: readonly AnnouncerMessage[];
}

const MAX_SNIPPET = 140;

function firstSentence(content: string): string {
  const plain = content
    .replace(/```[\s\S]*?```/g, ' ')
    .replace(/[*_`#>]+/g, '')
    .replace(/\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/\s+/g, ' ')
    .trim();
  const match = plain.match(/^.*?[.!?](?=\s|$)/);
  const sentence = match ? match[0] : plain;
  return sentence.length > MAX_SNIPPET
    ? `${sentence.slice(0, MAX_SNIPPET).trimEnd()}…`
    : sentence;
}

/**
 * Visually hidden polite live region for the main transcript. Silent while a
 * turn streams; announces once ("Answer ready. <first sentence>") when the
 * turn that was streaming on this thread completes. The announcement waits
 * for the assistant message to be present, then clears its pending flag so
 * later reconciliation re-renders never re-announce.
 */
interface AnnouncerState {
  threadId: string | null;
  wasStreaming: boolean;
  pending: boolean;
  text: string;
}

export function ChatCompletionAnnouncer({
  isStreaming,
  threadId,
  messages,
}: ChatCompletionAnnouncerProps): ReactElement {
  const [state, setState] = useState<AnnouncerState>({
    threadId,
    wasStreaming: isStreaming,
    pending: false,
    text: '',
  });

  const last = messages[messages.length - 1];
  const lastAssistantContent = last?.role === 'assistant' ? last.content : '';

  // Derived during render (React's "adjust state on prop change" pattern),
  // so no effect has to set state.
  let next: AnnouncerState = state;
  if (state.threadId !== threadId) {
    next = { threadId, wasStreaming: isStreaming, pending: false, text: '' };
  } else if (isStreaming) {
    next = { ...state, wasStreaming: true, pending: false, text: '' };
  } else {
    let pending = state.pending || state.wasStreaming;
    let text = state.text;
    if (pending && lastAssistantContent.trim()) {
      pending = false;
      text = `Answer ready. ${firstSentence(lastAssistantContent)}`;
    }
    next = { ...state, wasStreaming: false, pending, text };
  }
  if (
    next.threadId !== state.threadId ||
    next.wasStreaming !== state.wasStreaming ||
    next.pending !== state.pending ||
    next.text !== state.text
  ) {
    setState(next);
  }

  return (
    <div
      data-testid="chat-completion-announcer"
      className="sr-only"
      role="status"
      aria-live="polite"
      aria-atomic="true"
    >
      {next.text}
    </div>
  );
}
