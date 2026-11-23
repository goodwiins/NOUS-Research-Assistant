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
 * 1. Pages that can drop the param from the address bar right after reading
 *    it (`stripUrlParams`): `/search` drops `q`, `/verify-email` drops
 *    `token_hash`.
 * 2. While the URL still carries one (cli-auth keeps `session_id` so refresh
 *    and the login round trip keep working), replay is stopped WITHOUT
 *    sending, which discards its buffer, and buffering restarts with a fresh
 *    snapshot of the clean URL once the param is gone (`syncReplayWithUrl`).
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
 * Remove `keys` from the current URL in place. Passes Next's history state
 * through so back/forward keep working; Next syncs `useSearchParams` with
 * native `replaceState`.
 */
export function stripUrlParams(keys: readonly string[]): void {
  if (typeof window === 'undefined') return;
  const url = new URL(window.location.href);
  const present = keys.filter((key) => url.searchParams.has(key));
  if (present.length === 0) return;
  for (const key of present) url.searchParams.delete(key);
  window.history.replaceState(
    window.history.state,
    '',
    `${url.pathname}${url.search}${url.hash}`
  );
}

/** The slice of the replay integration the guard drives. */
export interface ReplayControl {
  getRecordingMode(): 'session' | 'buffer' | undefined;
  stop(options?: { flush?: boolean }): Promise<void>;
  startBuffering(): void;
}

let pausedByGuard = false;
let queue: Promise<void> = Promise.resolve();

/**
 * Stop replay (discarding, never sending, what it holds) while `href` is
 * sensitive; resume buffering with a fresh snapshot once it is clean. Calls
 * are serialised so a quick sensitive → clean change cannot race the async
 * stop. Never throws.
 */
export function syncReplayWithUrl(
  href: string,
  replay: ReplayControl | undefined
): Promise<void> {
  queue = queue.then(async () => {
    if (!replay) return;
    try {
      if (hasSensitiveUrlParams(href)) {
        if (replay.getRecordingMode() !== undefined) {
          pausedByGuard = true;
          await replay.stop({ flush: false });
        }
      } else if (pausedByGuard) {
        pausedByGuard = false;
        replay.startBuffering();
      }
    } catch {
      // Replay housekeeping must never break the page.
    }
  });
  return queue;
}

/** Test hook: forget the guard's pause state and queue. */
export function resetReplayPrivacyForTests(): void {
  pausedByGuard = false;
  queue = Promise.resolve();
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
