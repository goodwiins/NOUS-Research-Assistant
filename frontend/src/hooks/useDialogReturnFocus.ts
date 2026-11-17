import { useCallback, useLayoutEffect, useRef } from 'react';

/**
 * Restore focus to whatever element opened a controlled Radix dialog.
 *
 * Radix `Dialog` returns focus to its `DialogTrigger` on close. Dialogs that
 * are opened by state (a keyboard shortcut, a button elsewhere on the page)
 * have no trigger, so Radix focuses nothing and the keyboard user lands on
 * `<body>`. This hook records the focused element when `isOpen` turns true
 * (in a layout effect, before the dialog's own auto-focus moves it) and
 * returns an `onCloseAutoFocus` handler that sends focus back there.
 */
export function useDialogReturnFocus(
  isOpen: boolean
): (event: Event) => void {
  const returnTo = useRef<HTMLElement | null>(null);

  useLayoutEffect(() => {
    if (isOpen && typeof document !== 'undefined') {
      const active = document.activeElement;
      returnTo.current =
        active instanceof HTMLElement && active !== document.body
          ? active
          : null;
    }
  }, [isOpen]);

  return useCallback((event: Event) => {
    const target = returnTo.current;
    returnTo.current = null;
    if (target && target.isConnected) {
      event.preventDefault();
      target.focus();
    }
  }, []);
}
