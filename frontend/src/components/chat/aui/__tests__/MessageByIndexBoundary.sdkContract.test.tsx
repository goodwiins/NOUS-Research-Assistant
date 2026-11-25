import { afterEach, describe, expect, it, vi } from 'vitest';
import React from 'react';
import { ThreadPrimitive } from '@assistant-ui/react';
import { render, screen, type RenderResult } from '@/test/test-utils';

import { ChatRuntimeProvider } from '../ChatRuntimeProvider';
import {
  MessageByIndexBoundary,
  TRANSIENT_INDEX_ERROR_PATTERN,
} from '../AuiMessage';

const noop = vi.fn();

afterEach(() => {
  vi.restoreAllMocks();
});

function Row(): React.ReactElement {
  return <div>sdk row</div>;
}

/** Outer catcher: records whatever escapes, so an unswallowed SDK throw shows
 * up as a rendered message instead of failing render() opaquely. */
class EscapedErrorCatcher extends React.Component<
  { children: React.ReactNode },
  { error: Error | null }
> {
  state: { error: Error | null } = { error: null };

  static getDerivedStateFromError(error: Error): { error: Error } {
    return { error };
  }

  render(): React.ReactNode {
    const { error } = this.state;
    if (error) return <div data-testid="escaped-error">{error.message}</div>;
    return this.props.children;
  }
}

/** An empty runtime thread addressed at index 0: the real
 * @assistant-ui/store `useClientLookup` throws its out-of-bounds error during
 * render. No SDK mock stands in for the throw. */
function renderOutOfBoundsRow(
  children: (row: React.ReactNode) => React.ReactNode
): RenderResult {
  const row = (
    <ThreadPrimitive.MessageByIndex index={0} components={{ Message: Row }} />
  );
  return render(
    <ChatRuntimeProvider
      messages={[]}
      isRunning={false}
      onSend={noop}
      onCancel={noop}
    >
      <EscapedErrorCatcher>{children(row)}</EscapedErrorCatcher>
    </ChatRuntimeProvider>
  );
}

describe('MessageByIndexBoundary SDK contract', () => {
  // Guards the version-coupled wording match in TRANSIENT_INDEX_ERROR_PATTERN:
  // if an @assistant-ui bump rewords (or stops throwing) the transient lookup
  // error, these fail instead of the #1096 thread-switch crash returning.
  it('the installed SDK throws a transient lookup error the pattern matches', () => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
    renderOutOfBoundsRow((row) => row);

    const escaped = screen.getByTestId('escaped-error').textContent ?? '';
    expect(escaped).toMatch(/useClientLookup/);
    expect(escaped).toMatch(TRANSIENT_INDEX_ERROR_PATTERN);
  });

  it('the boundary swallows the real SDK out-of-bounds throw', () => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
    renderOutOfBoundsRow((row) => (
      <MessageByIndexBoundary resetKey="empty">{row}</MessageByIndexBoundary>
    ));

    expect(screen.queryByTestId('escaped-error')).not.toBeInTheDocument();
    expect(screen.queryByText('sdk row')).not.toBeInTheDocument();
  });
});
