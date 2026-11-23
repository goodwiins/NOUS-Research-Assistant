/**
 * GOO-637 (FC-2): the widget approval card must not focus Approve on mount,
 * or a stray Enter/Space typed while the card appears approves a tool run.
 */
import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { ConfirmationCard } from '../ConfirmationCard';

function renderCard(onConfirm = vi.fn()): ReturnType<typeof vi.fn> {
  render(
    <ConfirmationCard
      tools={[{ name: 'ingest_arxiv_papers', args: { query: 'x' } }]}
      message="The agent wants to perform an action."
      onConfirm={onConfirm}
      onCancel={vi.fn()}
      isLoading={false}
    />
  );
  return onConfirm;
}

const approveButton = (): HTMLElement =>
  screen.getByRole('button', { name: 'Approve and run the requested action' });

describe('ConfirmationCard focus safety', () => {
  it('focuses the dialog, not Approve, when the card mounts', () => {
    renderCard();
    expect(screen.getByRole('alertdialog')).toHaveFocus();
    expect(approveButton()).not.toHaveFocus();
  });

  it('ignores a stray Enter or Space right after the card mounts', async () => {
    const user = userEvent.setup();
    const onConfirm = renderCard();
    await user.keyboard('{Enter}');
    await user.keyboard(' ');
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it('still approves after an explicit Tab to Approve', async () => {
    const user = userEvent.setup();
    const onConfirm = renderCard();
    await user.tab();
    expect(approveButton()).toHaveFocus();
    await user.keyboard('{Enter}');
    expect(onConfirm).toHaveBeenCalledTimes(1);
  });
});
