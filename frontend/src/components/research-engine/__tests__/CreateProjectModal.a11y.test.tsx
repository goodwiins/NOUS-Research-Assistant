import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import {
  render,
  screen,
  waitFor,
  type RenderResult,
} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState, type ReactElement } from 'react';
import { describe, expect, it, vi } from 'vitest';

import { CreateProjectModal } from '../CreateProjectModal';

vi.mock('@/services/researchEngineService', () => ({
  createProject: vi.fn(),
}));
vi.mock('@/services/projectService', () => ({
  listWorkflowLinkOptions: vi.fn().mockResolvedValue([
    { id: 'c1', name: 'Project One' },
  ]),
}));

function Harness(): ReactElement {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>
        Open workflow
      </button>
      <CreateProjectModal
        isOpen={open}
        onClose={() => setOpen(false)}
        onCreated={() => {}}
      />
    </>
  );
}

function renderHarness(): RenderResult {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <Harness />
    </QueryClientProvider>
  );
}

describe('CreateProjectModal focus handling (GOO-599)', () => {
  it('is a named modal dialog that hides the page behind it', async () => {
    const user = userEvent.setup();
    renderHarness();

    await user.click(screen.getByRole('button', { name: 'Open workflow' }));

    const dialog = await screen.findByRole('dialog', {
      name: 'Enable Research Workflow',
    });
    expect(dialog).toBeInTheDocument();
    // The opener is outside the modal and no longer reachable.
    expect(
      screen.queryByRole('button', { name: 'Open workflow' })
    ).not.toBeInTheDocument();
    expect(dialog.contains(document.activeElement)).toBe(true);
  });

  it('keeps Tab inside the dialog', async () => {
    const user = userEvent.setup();
    renderHarness();
    await user.click(screen.getByRole('button', { name: 'Open workflow' }));
    const dialog = await screen.findByRole('dialog');

    for (let i = 0; i < 6; i += 1) {
      await user.tab();
      expect(dialog.contains(document.activeElement)).toBe(true);
    }
  });

  it('closes on Escape and restores focus to the opener', async () => {
    const user = userEvent.setup();
    renderHarness();
    const opener = screen.getByRole('button', { name: 'Open workflow' });
    await user.click(opener);
    await screen.findByRole('dialog');

    await user.keyboard('{Escape}');

    await waitFor(() =>
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    );
    expect(document.activeElement).toBe(opener);
  });
});
