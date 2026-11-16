import React, { useState } from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, type RenderResult } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import { QuickSearch } from '../QuickSearch';
import { KeyboardShortcuts } from '../KeyboardShortcuts';
import { CreateProjectModal } from '@/components/research-engine/CreateProjectModal';

const push = vi.fn();
vi.mock('next/navigation', () => ({
  useRouter: () => ({ push }),
}));

vi.mock('@/services/researchEngineService', () => ({
  createProject: vi.fn(),
}));
vi.mock('@/services/projectService', () => ({
  listWorkflowLinkOptions: vi.fn().mockResolvedValue([
    { id: 'c1', name: 'Project one' },
  ]),
}));

type OverlayProps = { isOpen: boolean; onClose: () => void };

// Renders a trigger button, a focusable element behind the overlay, and the
// overlay itself, so tests can check trap and restore against real elements.
function Harness({
  Overlay,
  onClose,
}: {
  Overlay: React.ComponentType<OverlayProps>;
  onClose?: () => void;
}): React.ReactElement {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>
        Open overlay
      </button>
      <a href="#behind">Link behind overlay</a>
      <Overlay
        isOpen={open}
        onClose={() => {
          onClose?.();
          setOpen(false);
        }}
      />
    </>
  );
}

function CreateProjectOverlay(props: OverlayProps): React.ReactElement {
  return <CreateProjectModal {...props} onCreated={() => {}} />;
}

function renderWithQuery(ui: React.ReactElement): RenderResult {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>{ui}</QueryClientProvider>
  );
}

const cases: Array<{
  name: string;
  Overlay: React.ComponentType<OverlayProps>;
  dialogName: RegExp;
}> = [
  { name: 'QuickSearch', Overlay: QuickSearch, dialogName: /quick search/i },
  {
    name: 'KeyboardShortcuts',
    Overlay: KeyboardShortcuts,
    dialogName: /keyboard shortcuts/i,
  },
  {
    name: 'CreateProjectModal',
    Overlay: CreateProjectOverlay,
    dialogName: /enable research workflow/i,
  },
];

describe.each(cases)('$name focus handling', ({ Overlay, dialogName }) => {
  beforeEach(() => {
    push.mockReset();
  });

  it('is a named modal dialog that takes focus on open', async () => {
    const user = userEvent.setup();
    renderWithQuery(<Harness Overlay={Overlay} />);

    await user.click(screen.getByRole('button', { name: 'Open overlay' }));

    const dialog = await screen.findByRole('dialog', { name: dialogName });
    // Radix makes the dialog modal by hiding everything else from assistive
    // tech rather than by setting aria-modal.
    const behind = screen.getByRole('link', {
      name: 'Link behind overlay',
      hidden: true,
    });
    expect(behind.closest('[aria-hidden="true"]')).not.toBeNull();
    await waitFor(() => expect(dialog).toContainElement(document.activeElement as HTMLElement));
  });

  it('keeps Tab and Shift+Tab inside the dialog', async () => {
    const user = userEvent.setup();
    renderWithQuery(<Harness Overlay={Overlay} />);

    await user.click(screen.getByRole('button', { name: 'Open overlay' }));
    const dialog = await screen.findByRole('dialog', { name: dialogName });

    for (let i = 0; i < 6; i += 1) {
      await user.tab();
      expect(dialog).toContainElement(document.activeElement as HTMLElement);
    }
    for (let i = 0; i < 6; i += 1) {
      await user.tab({ shift: true });
      expect(dialog).toContainElement(document.activeElement as HTMLElement);
    }
  });

  it('closes on Escape and returns focus to the trigger', async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    renderWithQuery(<Harness Overlay={Overlay} onClose={onClose} />);

    const trigger = screen.getByRole('button', { name: 'Open overlay' });
    await user.click(trigger);
    await screen.findByRole('dialog', { name: dialogName });

    await user.keyboard('{Escape}');

    expect(onClose).toHaveBeenCalledTimes(1);
    await waitFor(() =>
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    );
    await waitFor(() => expect(trigger).toHaveFocus());
  });
});

describe('QuickSearch', () => {
  it('focuses the search input on open', async () => {
    const user = userEvent.setup();
    renderWithQuery(<Harness Overlay={QuickSearch} />);

    await user.click(screen.getByRole('button', { name: 'Open overlay' }));

    await waitFor(() =>
      expect(screen.getByRole('textbox', { name: 'Search query' })).toHaveFocus()
    );
  });
});

describe('KeyboardShortcuts', () => {
  it('closes on "?" without letting a window listener see it', async () => {
    const user = userEvent.setup();
    const windowListener = vi.fn();
    window.addEventListener('keydown', windowListener);
    try {
      renderWithQuery(<Harness Overlay={KeyboardShortcuts} />);
      await user.click(screen.getByRole('button', { name: 'Open overlay' }));
      await screen.findByRole('dialog', { name: /keyboard shortcuts/i });
      windowListener.mockClear();

      await user.keyboard('?');

      await waitFor(() =>
        expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
      );
      expect(
        windowListener.mock.calls.some(([e]) => (e as KeyboardEvent).key === '?')
      ).toBe(false);
    } finally {
      window.removeEventListener('keydown', windowListener);
    }
  });
});
