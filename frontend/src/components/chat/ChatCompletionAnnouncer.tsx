'use client';

import { useState, type ReactElement } from 'react';

interface AnnouncerMessage {
  role: 'user' | 'assistant';
  content: string;
  metadata?: { stopped?: boolean };
  /** Set on a failed turn; the error alert already speaks, so stay silent. */
  error?: unknown;
  /** Set while the turn is paused for a HITL confirmation. */
  pendingApproval?: unknown;
}

interface ChatCompletionAnnouncerProps {
  /** True while the displayed thread's turn is still growing. */
  isStreaming: boolean;
  threadId: string | null;
  messages: readonly AnnouncerMessage[];
}

interface AnnouncerState {
  threadId: string | null;
  wasStreaming: boolean;
  pending: boolean;
  text: string;
}

/**
 * Visually hidden polite live region for the main transcript. Silent while a
 * turn streams; announces once when the turn that was streaming on this
 * thread ends: "Answer ready." or, for a user-stopped turn, "Response
 * stopped.". Errored and confirmation-paused turns stay silent (their own
 * alert/approval UI speaks) and clear the pending flag. The answer text is
 * deliberately never read out: it is markdown and not safe to flatten.
 */
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
  const lastAssistant = last?.role === 'assistant' ? last : undefined;

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
    if (pending && lastAssistant) {
      if (lastAssistant.error || lastAssistant.pendingApproval) {
        pending = false;
      } else if (lastAssistant.metadata?.stopped) {
        pending = false;
        text = 'Response stopped.';
      } else if (lastAssistant.content.trim()) {
        pending = false;
        text = 'Answer ready.';
      }
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
