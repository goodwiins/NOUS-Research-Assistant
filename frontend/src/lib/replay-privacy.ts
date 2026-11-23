/**
 * Keep page URLs that carry secrets or user text out of Sentry session replay
 * (GOO-734).
 *
 * Replay's `beforeAddRecordingEvent` only sees custom events (see
 * `scrubReplayRecordingEvent`). Two things never reach it:
 *
 * - the rrweb Meta event, which records `location.href` on every full
 *   snapshot. In on-error mode replay buffers from page load, so the first
 *   snapshot of `/verify-email?token_hash=…` or `/search?q=…` holds the full
 *   URL;
 * - `href` attributes in the recorded DOM.
 *
 * Defences, in order:
 *
 * 1. One-time secrets with no use in the address bar are dropped from it
 *    right after they are read (`stripUrlParams`): `/verify-email` drops
 *    `token_hash` and `type`.
 * 2. While the URL carries a sensitive param, replay is stopped WITHOUT
 *    sending, which discards its buffer, and buffering restarts with a fresh
 *    snapshot of the clean URL once the param is gone. This covers URLs that
 *    keep their param on purpose: `/search?q=` (refresh, Back and bookmarks
 *    need it), cli-auth's `session_id` and the ORCID callback's `code`.
 *    `installReplayUrlGuard` checks every history change: it wraps
 *    `pushState`/`replaceState` before Next patches them, so Next's own
 *    navigations and history writes pass through it, and replay is stopped
 *    BEFORE the address bar shows a sensitive URL.
 * 3. Links whose target carries a query are blocked (`REPLAY_BLOCK_SELECTORS`):
 *    recorded as empty placeholders, with no `href`.
 *
 * This module deliberately imports no Sentry runtime: callers pass the replay
 * integration, so the browser test can drive the real SDK with these exact
 * options.
 */
import { scrubReplayRecordingEvent } from './sentry-scrub';

/** Query/fragment params whose value is a credential or user-entered text. */
export const SENSITIVE_URL_PARAMS: ReadonlySet<string> = new Set([
  'q',
  'query',
  'code',
  'token',
  'token_hash',
  'session_id',
  'access_token',
  'refresh_token',
  'provider_token',
  'provider_refresh_token',
]);

/** Params whose value is itself a URL (`/login?next=/cli-auth?session_id=…`). */
const NESTED_URL_PARAMS = ['next', 'redirect', 'redirect_to', 'returnTo'];
const MAX_NESTING = 2;
const PARSE_BASE = 'http://local.invalid';

function paramsHaveSensitive(params: URLSearchParams, depth: number): boolean {
  for (const key of params.keys()) {
    if (SENSITIVE_URL_PARAMS.has(key.toLowerCase())) return true;
  }
  if (depth >= MAX_NESTING) return false;
  return NESTED_URL_PARAMS.some((key) => {
    const nested = params.get(key);
    return nested !== null && hrefHasSensitive(nested, depth + 1);
  });
}

function hrefHasSensitive(href: string, depth: number): boolean {
  let url: URL;
  try {
    url = new URL(href, PARSE_BASE);
  } catch {
    // Unparseable: assume the worst rather than record it.
    return true;
  }
  if (paramsHaveSensitive(url.searchParams, depth)) return true;
  const fragment = url.hash.slice(1);
  return fragment.includes('=')
    ? paramsHaveSensitive(new URLSearchParams(fragment), depth)
    : false;
}

/** True when the URL's query or fragment carries a sensitive param. */
export function hasSensitiveUrlParams(href: string): boolean {
  return hrefHasSensitive(href, 0);
}

/**
 * Remove `keys` from the current URL in place.
 *
 * The state must be `null`, not `history.state`: Next's patched
 * `replaceState` skips calls whose state carries its own `__NA` marker, so
 * passing Next's state through would change the address bar but not the
 * router, and the router would write the old URL back on its next update.
 * With `null`, Next copies its internal state into the entry (back/forward
 * keep working) and moves its own URL, so `useSearchParams`, `refresh()` and
 * later navigations no longer carry the param.
 */
export function stripUrlParams(keys: readonly string[]): void {
  if (typeof window === 'undefined') return;
  const url = new URL(window.location.href);
  const present = keys.filter((key) => url.searchParams.has(key));
  if (present.length === 0) return;
  for (const key of present) url.searchParams.delete(key);
  window.history.replaceState(null, '', `${url.pathname}${url.search}${url.hash}`);
}

/** The slice of the replay integration the guard drives. */
export interface ReplayControl {
  getRecordingMode(): 'session' | 'buffer' | undefined;
  stop(options?: { flush?: boolean }): Promise<void>;
  startBuffering(): void;
}

let pausedByGuard = false;

/**
 * Stop replay (discarding, never sending, what it holds) while `href` is
 * sensitive; resume buffering with a fresh snapshot once it is clean.
 *
 * Synchronous: without a flush the SDK's `stop()` disables recording and
 * drops its buffer before it first awaits (SDK 10.78), so a caller can stop
 * replay and only then change the URL. Never throws.
 */
export function syncReplayWithUrl(
  href: string,
  replay: ReplayControl | undefined
): void {
  if (!replay) return;
  try {
    if (hasSensitiveUrlParams(href)) {
      if (replay.getRecordingMode() !== undefined) {
        pausedByGuard = true;
        void replay.stop({ flush: false }).catch(() => undefined);
      }
    } else if (pausedByGuard) {
      pausedByGuard = false;
      replay.startBuffering();
    }
  } catch {
    // Replay housekeeping must never break the page.
  }
}

let installed = false;
let uninstall: (() => void) | undefined;

/**
 * Check every URL change, then the current URL (GOO-734).
 *
 * Call it once, right after `Sentry.init` and before the app hydrates. Next
 * patches `history.pushState`/`replaceState` when its router mounts and
 * calls whatever it found, so wrapping them first puts every history change
 * through this guard: client navigations, `router.refresh()`, Next's own
 * URL writes and code calling `history` directly. A change TO a sensitive
 * URL stops replay before the address bar changes; a change to a clean URL
 * resumes it afterwards. Back/forward arrive as `popstate`; in the app,
 * Next's restore then also writes the URL with `replaceState`, which passes
 * the wrapper too (the listener covers entries Next does not manage).
 */
export function installReplayUrlGuard(
  getReplay: () => ReplayControl | undefined
): void {
  if (typeof window === 'undefined' || installed) return;
  installed = true;
  const { history } = window;
  const natives = { pushState: history.pushState, replaceState: history.replaceState };
  const check = (href: string): void => {
    try {
      syncReplayWithUrl(href, getReplay());
    } catch {
      // Never break navigation.
    }
  };
  for (const method of ['pushState', 'replaceState'] as const) {
    const native = history[method].bind(history);
    history[method] = function guardedHistoryChange(
      data: unknown,
      unused: string,
      url?: string | URL | null
    ): void {
      // Relative and unparseable URLs are handled by hasSensitiveUrlParams
      // (an unparseable one counts as sensitive).
      if (url !== undefined && url !== null && hasSensitiveUrlParams(String(url))) {
        check(String(url));
      }
      native(data, unused, url);
      check(window.location.href);
    };
  }
  const onUrlChange = (): void => check(window.location.href);
  window.addEventListener('popstate', onUrlChange);
  window.addEventListener('hashchange', onUrlChange);
  uninstall = () => {
    history.pushState = natives.pushState;
    history.replaceState = natives.replaceState;
    window.removeEventListener('popstate', onUrlChange);
    window.removeEventListener('hashchange', onUrlChange);
  };
  check(window.location.href);
}

/** Test hook: forget the pause state and undo `installReplayUrlGuard`. */
export function resetReplayPrivacyForTests(): void {
  pausedByGuard = false;
  uninstall?.();
  uninstall = undefined;
  installed = false;
}

/**
 * Links whose target carries a query, or a fragment with params, are recorded
 * as empty placeholders. `maskAttributes` cannot hide them: rrweb rewrites
 * `href`/`src` to absolute URLs and never applies the attribute mask to them
 * (SDK 10.78 `transformAttribute`), so `block` is the option that works.
 * Stylesheet `<link>`s are left alone so replays keep their styling.
 */
export const REPLAY_BLOCK_SELECTORS = [
  'a[href*="?"]',
  'area[href*="?"]',
  'a[href*="#"][href*="="]',
  'area[href*="#"][href*="="]',
] as const;

/**
 * Privacy options for `replayIntegration`, shared by `instrumentation-client`
 * and the browser test so both record with exactly these settings.
 */
export const REPLAY_PRIVACY_OPTIONS = {
  maskAllText: true,
  blockAllMedia: true,
  block: [...REPLAY_BLOCK_SELECTORS],
  // Strip query strings from navigation, resource and request frames.
  beforeAddRecordingEvent: scrubReplayRecordingEvent,
};
