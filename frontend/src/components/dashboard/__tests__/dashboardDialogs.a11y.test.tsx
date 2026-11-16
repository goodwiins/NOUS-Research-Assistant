import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState, type ComponentType, type ReactElement } from 'react';
import { describe, expect, it, vi } from 'vitest';

import { KeyboardShortcuts } from '../KeyboardShortcuts';
import { QuickSearch } from '../QuickSearch';

vi.mock('next/navigation', () => ({
  useRouter: () => ({ push: vi.fn() }),
}));

type OverlayProps = { isOpen: boolean; onClose: () => void };

function Harness({
  Overlay,
}: {
  Overlay: ComponentType<OverlayProps>;
}): ReactElement {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>
        Opener
      </button>
      <button type="button">Behind</button>
      <Overlay isOpen={open} onClose={() => setOpen(false)} />
    </>
  );
}

describe.each([
  ['QuickSearch', QuickSearch, 'Quick search'],
  ['KeyboardShortcuts', KeyboardShortcuts, 'Keyboard Shortcuts'],
] as const)('%s focus handling (GOO-599)', (_name, Overlay, dialogName) => {
  it('traps Tab inside the dialog and hides the page behind it', async () => {
    const user = userEvent.setup();
    render(<Harness Overlay={Overlay} />);
    await user.click(screen.getByRole('button', { name: 'Opener' }));

    const dialog = await screen.findByRole('dialog', { name: dialogName });
    expect(dialog.contains(document.activeElement)).toBe(true);
    expect(
      screen.queryByRole('button', { name: 'Behind' })
    ).not.toBeInTheDocument();

    for (let i = 0; i < 5; i += 1) {
      await user.tab();
      expect(dialog.contains(document.activeElement)).toBe(true);
    }
  });

  it('closes on Escape and restores focus to the opener', async () => {
    const user = userEvent.setup();
    render(<Harness Overlay={Overlay} />);
    const opener = screen.getByRole('button', { name: 'Opener' });
    await user.click(opener);
    await screen.findByRole('dialog', { name: dialogName });

    await user.keyboard('{Escape}');

    await waitFor(() =>
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    );
    expect(document.activeElement).toBe(opener);
  });
});

it('QuickSearch focuses the search input on open', async () => {
  const user = userEvent.setup();
  render(<Harness Overlay={QuickSearch} />);
  await user.click(screen.getByRole('button', { name: 'Opener' }));
  await screen.findByRole('dialog', { name: 'Quick search' });
  expect(document.activeElement).toBe(
    screen.getByRole('textbox', { name: 'Search query' })
  );
});
