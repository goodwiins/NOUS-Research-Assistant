import { useCallback, useLayoutEffect, useRef } from 'react';

/**
 * Focus restore for a controlled Radix Dialog opened without a
 * `DialogTrigger`.
 *
 * Radix returns focus to its own trigger ref on close. When the dialog is
 * opened from state (a button elsewhere, a keyboard shortcut), that ref is
 * empty and focus falls to `<body>`. This hook records the element that had
 * focus when `isOpen` became true and returns an `onCloseAutoFocus` handler
 * that moves focus back to it.
 *
 * The record is taken in a layout effect so it runs before the dialog's
 * focus scope (a passive effect in a child) moves focus into the dialog.
 */
export function useReturnFocusOnClose(
  isOpen: boolean
): (event: Event) => void {
  const openerRef = useRef<HTMLElement | null>(null);

  useLayoutEffect(() => {
    if (!isOpen || typeof document === 'undefined') return;
    const active = document.activeElement;
    openerRef.current =
      active instanceof HTMLElement && active !== document.body ? active : null;
  }, [isOpen]);

  return useCallback((event: Event) => {
    const opener = openerRef.current;
    openerRef.current = null;
    if (opener && opener.isConnected) {
      event.preventDefault();
      opener.focus();
    }
  }, []);
}
