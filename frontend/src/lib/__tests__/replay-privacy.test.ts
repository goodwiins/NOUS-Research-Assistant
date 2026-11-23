import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  hasSensitiveUrlParams,
  resetReplayPrivacyForTests,
  stripUrlParams,
  syncReplayWithUrl,
  type ReplayControl,
} from '@/lib/replay-privacy';

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

  it('removes only the named params and keeps history state and fragment', () => {
    const before = window.history.length;
    stripUrlParams(['token_hash', 'type']);
    expect(`${window.location.pathname}${window.location.search}${window.location.hash}`).toBe(
      '/verify-email?error=x#frag'
    );
    expect(window.history.state).toEqual({ __next: 'state' });
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

describe('syncReplayWithUrl', () => {
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
    } satisfies ReplayControl;
    return control;
  };

  beforeEach(() => resetReplayPrivacyForTests());
  afterEach(() => resetReplayPrivacyForTests());

  it('discards (never flushes) the buffer on a sensitive URL and resumes once clean', async () => {
    const replay = makeReplay();
    await syncReplayWithUrl('/verify-email?token_hash=abc', replay);
    expect(replay.stop).toHaveBeenCalledWith({ flush: false });
    expect(replay.startBuffering).not.toHaveBeenCalled();
    await syncReplayWithUrl('/verify-email', replay);
    expect(replay.startBuffering).toHaveBeenCalledTimes(1);
    // Clean again: nothing more to do.
    await syncReplayWithUrl('/dashboard', replay);
    expect(replay.startBuffering).toHaveBeenCalledTimes(1);
  });

  it('also stops a replay already recording as a session', async () => {
    const replay = makeReplay('session');
    await syncReplayWithUrl('/search?q=secret', replay);
    expect(replay.stop).toHaveBeenCalledWith({ flush: false });
  });

  it('never starts a replay it did not stop', async () => {
    const replay = makeReplay('off');
    await syncReplayWithUrl('/search?q=secret', replay);
    expect(replay.stop).not.toHaveBeenCalled();
    await syncReplayWithUrl('/search', replay);
    expect(replay.startBuffering).not.toHaveBeenCalled();
  });

  it('serialises a fast sensitive → clean change behind the async stop', async () => {
    const order: string[] = [];
    let finishStop!: () => void;
    const replay: ReplayControl = {
      getRecordingMode: () => 'buffer',
      stop: vi.fn(
        () =>
          new Promise<void>((done) => {
            finishStop = () => {
              order.push('stopped');
              done();
            };
          })
      ),
      startBuffering: vi.fn(() => order.push('buffering')),
    };
    const first = syncReplayWithUrl('/search?q=secret', replay);
    const second = syncReplayWithUrl('/search', replay);
    await vi.waitFor(() => expect(replay.stop).toHaveBeenCalled());
    expect(replay.startBuffering).not.toHaveBeenCalled();
    finishStop();
    await Promise.all([first, second]);
    expect(order).toEqual(['stopped', 'buffering']);
  });

  it('is a no-op without replay and never throws', async () => {
    await expect(syncReplayWithUrl('/search?q=x', undefined)).resolves.toBeUndefined();
    const broken: ReplayControl = {
      getRecordingMode: () => {
        throw new Error('boom');
      },
      stop: vi.fn(),
      startBuffering: vi.fn(),
    };
    await expect(syncReplayWithUrl('/search?q=x', broken)).resolves.toBeUndefined();
  });
});
