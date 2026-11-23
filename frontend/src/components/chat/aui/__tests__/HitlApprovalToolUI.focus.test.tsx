/**
 * GOO-637 (FC-2): the approval gate must not focus Approve on mount, or a
 * stray Enter/Space typed while the card appears approves a tool run.
 */
import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { HitlApprovalRenderer } from '../HitlApprovalToolUI';

function renderGate(
  respondToApproval = vi.fn()
): ReturnType<typeof vi.fn> {
  render(
    <HitlApprovalRenderer
      args={{ toolName: 'ingest_arxiv_papers', toolArgs: { n: 2 } }}
      approval={{ id: 'a1' }}
      respondToApproval={respondToApproval}
    />
  );
  return respondToApproval;
}

describe('HitlApprovalRenderer focus safety', () => {
  it('focuses the dialog, not Approve, when the gate appears', () => {
    renderGate();
    expect(screen.getByRole('alertdialog')).toHaveFocus();
    expect(screen.getByRole('button', { name: 'Approve' })).not.toHaveFocus();
  });

  it('ignores a stray Enter or Space right after the gate appears', async () => {
    const user = userEvent.setup();
    const respondToApproval = renderGate();
    await user.keyboard('{Enter}');
    await user.keyboard(' ');
    expect(respondToApproval).not.toHaveBeenCalled();
  });

  it('still approves after an explicit Tab to Approve', async () => {
    const user = userEvent.setup();
    const respondToApproval = renderGate();
    await user.tab();
    expect(screen.getByRole('button', { name: 'Approve' })).toHaveFocus();
    await user.keyboard('{Enter}');
    expect(respondToApproval).toHaveBeenCalledWith({ approved: true });
  });
});
