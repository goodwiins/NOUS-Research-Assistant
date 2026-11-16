/**
 * True when the page is in a background tab or minimised window.
 *
 * Hand-written `setInterval` polls skip a tick while this is true, the same
 * way TanStack Query pauses `refetchInterval` (its
 * `refetchIntervalInBackground` defaults to false). The next visible tick
 * catches up, so nothing is lost.
 */
export function isTabHidden(): boolean {
  return (
    typeof document !== 'undefined' && document.visibilityState === 'hidden'
  );
}
