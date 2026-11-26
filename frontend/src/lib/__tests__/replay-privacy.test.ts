import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  REPLAY_BLOCK_SELECTORS,
  REPLAY_PRIVACY_OPTIONS,
  hasSensitiveUrlParams,
  installReplayUrlGuard,
  resetReplayPrivacyForTests,
  stripUrlParams,
  syncReplayWithUrl,
  type ReplayControl,
} from '@/lib/replay-privacy';
import { scrubReplayRecordingEvent } from '@/lib/sentry-scrub';

describe('hasSensitiveUrlParams', () => {
  it.each([
    '/search?q=lithium dose',
    '/verify-email?token_hash=abc&type=signup',
    '/cli-auth?session_id=s-1',
    '/auth/callback?code=xyz',
    '/login?next=%2Fcli-auth%3Fsession_id%3Ds-1',
    '/login?next=/cli-auth?session_id=s-1',
    'https://app.test/#access_token=abc&refresh_token=def',
    '/x?Q=upper-case-key',
  ])('flags %s', (href) => {
    expect(hasSensitiveUrlParams(href)).toBe(true);
  });

  it.each([
    '/chat',
    '/chat?thread=t-1&new=1',
    '/entities?tab=list&page=2',
    '/login?next=/dashboard',
    '/login?error=access_denied',
    '/docs#section-2',
    'https://app.test/projects/p-1?draftId=d-1',
  ])('allows %s', (href) => {
    expect(hasSensitiveUrlParams(href)).toBe(false);
  });
});

describe('stripUrlParams', () => {
  beforeEach(() => {
    window.history.replaceState({ __next: 'state' }, '', '/verify-email?token_hash=abc&type=signup&error=x#frag');
  });

  it('removes only the named params, keeps the fragment and hands Next a null state', () => {
    const before = window.history.length;
    const spy = vi.spyOn(window.history, 'replaceState');
    stripUrlParams(['token_hash', 'type']);
    expect(`${window.location.pathname}${window.location.search}${window.location.hash}`).toBe(
      '/verify-email?error=x#frag'
    );
    // Not Next's own state: its patched replaceState skips calls carrying
    // that state and would leave the router on the old URL.
    expect(spy).toHaveBeenCalledWith(null, '', '/verify-email?error=x#frag');
    spy.mockRestore();
    // Replaced, not pushed: back still leaves the page.
    expect(window.history.length).toBe(before);
  });

  it('does not touch history when nothing matches', () => {
    const spy = vi.spyOn(window.history, 'replaceState');
    stripUrlParams(['q']);
    expect(spy).not.toHaveBeenCalled();
    spy.mockRestore();
  });
});

const makeReplay = (
  mode: 'buffer' | 'session' | 'off' = 'buffer'
): ReplayControl & {
  getRecordingMode: ReturnType<typeof vi.fn>;
  stop: ReturnType<typeof vi.fn>;
  startBuffering: ReturnType<typeof vi.fn>;
} => {
  let current: 'buffer' | 'session' | undefined = mode === 'off' ? undefined : mode;
  const control = {
    getRecordingMode: vi.fn(() => current),
    stop: vi.fn(async () => {
      current = undefined;
    }),
    startBuffering: vi.fn(() => {
      current = 'buffer';
    }),
  };
  control.stop.mockImplementation(async () => {
    current = undefined;
  });
  return control as never;
};

describe('syncReplayWithUrl', () => {
  beforeEach(() => resetReplayPrivacyForTests());
  afterEach(() => resetReplayPrivacyForTests());

  it('discards (never flushes) the buffer on a sensitive URL and resumes once clean', () => {
    const replay = makeReplay();
    syncReplayWithUrl('/verify-email?token_hash=abc', replay);
    expect(replay.stop).toHaveBeenCalledWith({ flush: false });
    expect(replay.startBuffering).not.toHaveBeenCalled();
    syncReplayWithUrl('/verify-email', replay);
    expect(replay.startBuffering).toHaveBeenCalledTimes(1);
    // Clean again: nothing more to do.
    syncReplayWithUrl('/dashboard', replay);
    expect(replay.startBuffering).toHaveBeenCalledTimes(1);
  });

  it('also stops a replay already recording as a session', () => {
    const replay = makeReplay('session');
    syncReplayWithUrl('/search?q=secret', replay);
    expect(replay.stop).toHaveBeenCalledWith({ flush: false });
  });

  it('never starts a replay it did not stop', () => {
    const replay = makeReplay('off');
    syncReplayWithUrl('/search?q=secret', replay);
    expect(replay.stop).not.toHaveBeenCalled();
    syncReplayWithUrl('/search', replay);
    expect(replay.startBuffering).not.toHaveBeenCalled();
  });

  it('stops synchronously, before the caller changes the URL', () => {
    const replay = makeReplay();
    syncReplayWithUrl('/search?q=secret', replay);
    // No await: the stop has been requested and recording is off already.
    expect(replay.stop).toHaveBeenCalledTimes(1);
    expect(replay.getRecordingMode()).toBeUndefined();
  });

  it('is a no-op without replay and never throws', () => {
    expect(() => syncReplayWithUrl('/search?q=x', undefined)).not.toThrow();
    const broken: ReplayControl = {
      getRecordingMode: () => {
        throw new Error('boom');
      },
      stop: vi.fn(),
      startBuffering: vi.fn(),
    };
    expect(() => syncReplayWithUrl('/search?q=x', broken)).not.toThrow();
    const rejecting = makeReplay();
    rejecting.stop.mockRejectedValueOnce(new Error('boom'));
    expect(() => syncReplayWithUrl('/search?q=x', rejecting)).not.toThrow();
  });
});

describe('installReplayUrlGuard', () => {
  beforeEach(() => {
    resetReplayPrivacyForTests();
    window.history.replaceState(null, '', '/');
  });
  afterEach(() => {
    resetReplayPrivacyForTests();
    window.history.replaceState(null, '', '/');
  });

  it('checks the current URL on install', () => {
    window.history.replaceState(null, '', '/cli-auth?session_id=s-1');
    const replay = makeReplay();
    installReplayUrlGuard(() => replay);
    expect(replay.stop).toHaveBeenCalledWith({ flush: false });
  });

  it('stops replay BEFORE the address bar shows a sensitive URL', () => {
    const replay = makeReplay();
    installReplayUrlGuard(() => replay);
    let hrefWhenStopped = '';
    replay.stop.mockImplementation(async () => {
      hrefWhenStopped = window.location.href;
      replay.getRecordingMode.mockReturnValue(undefined);
    });
    window.history.pushState(null, '', '/search?q=secret');
    expect(replay.stop).toHaveBeenCalledTimes(1);
    expect(hrefWhenStopped).not.toContain('secret');
    expect(window.location.search).toBe('?q=secret');
  });

  it('resumes after a history change to a clean URL, including a wrapping router patch', () => {
    const replay = makeReplay();
    installReplayUrlGuard(() => replay);
    // Like Next: a later patch that calls the function it found.
    const found = window.history.replaceState.bind(window.history);
    window.history.replaceState = (data, unused, url) => found(data, unused, url);
    window.history.pushState(null, '', '/verify-email?token_hash=t');
    window.history.replaceState({ __NA: true }, '', '/verify-email');
    expect(replay.startBuffering).toHaveBeenCalledTimes(1);
    expect(window.location.pathname).toBe('/verify-email');
  });

  it('re-checks on back/forward', () => {
    const replay = makeReplay();
    installReplayUrlGuard(() => replay);
    // The browser moves the URL itself on Back, then fires popstate.
    const browserMoves = (path: string): void => {
      History.prototype.replaceState.call(window.history, null, '', path);
      window.dispatchEvent(new PopStateEvent('popstate'));
    };
    browserMoves('/search?q=secret');
    expect(replay.stop).toHaveBeenCalledTimes(1);
    browserMoves('/dashboard');
    expect(replay.startBuffering).toHaveBeenCalledTimes(1);
  });

  it('installs once and never breaks history when the guard throws', () => {
    const getReplay = vi.fn(() => {
      throw new Error('boom');
    });
    installReplayUrlGuard(getReplay);
    const wrapped = window.history.pushState;
    installReplayUrlGuard(getReplay);
    expect(window.history.pushState).toBe(wrapped);
    expect(() => window.history.pushState(null, '', '/search?q=x')).not.toThrow();
    expect(window.location.search).toBe('?q=x');
  });
});

describe('REPLAY_PRIVACY_OPTIONS (GOO-746, GOO-774)', () => {
  it('blocks every iframe', () => {
    // The browser spec proves the effect but runs on pull requests only;
    // this pins the selector on every unit run.
    expect(REPLAY_BLOCK_SELECTORS).toContain('iframe');
    expect(REPLAY_PRIVACY_OPTIONS.block).toContain('iframe');
  });

  it('masks text, blocks media and query links, and scrubs recorded URLs', () => {
    expect(REPLAY_PRIVACY_OPTIONS).toEqual({
      maskAllText: true,
      blockAllMedia: true,
      block: [...REPLAY_BLOCK_SELECTORS],
      beforeAddRecordingEvent: scrubReplayRecordingEvent,
    });
    expect(REPLAY_BLOCK_SELECTORS).toEqual(
      expect.arrayContaining(['a[href*="?"]', 'a[href*="#"][href*="="]'])
    );
  });
});
