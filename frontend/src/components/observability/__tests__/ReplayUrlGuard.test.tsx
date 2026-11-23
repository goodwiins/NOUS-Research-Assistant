import { afterEach, describe, expect, it, vi } from 'vitest';
import { render } from '@testing-library/react';
import { resetReplayPrivacyForTests } from '@/lib/replay-privacy';
import { ReplayUrlGuard } from '../ReplayUrlGuard';

let mode: 'buffer' | undefined = 'buffer';
const replay = {
  getRecordingMode: vi.fn(() => mode),
  stop: vi.fn(async () => {
    mode = undefined;
  }),
  startBuffering: vi.fn(() => {
    mode = 'buffer';
  }),
};

vi.mock('@sentry/nextjs', () => ({ getReplay: () => replay }));

vi.mock('next/navigation', () => ({
  usePathname: () => window.location.pathname,
  useSearchParams: () => new URLSearchParams(window.location.search),
}));

describe('ReplayUrlGuard (GOO-734)', () => {
  afterEach(() => {
    resetReplayPrivacyForTests();
    vi.clearAllMocks();
    mode = 'buffer';
    window.history.replaceState(null, '', '/');
  });

  it('stops replay on a sensitive URL and resumes once the query is gone', async () => {
    window.history.replaceState(null, '', '/cli-auth?session_id=s-1');
    const { rerender } = render(<ReplayUrlGuard />);
    await vi.waitFor(() => expect(replay.stop).toHaveBeenCalledWith({ flush: false }));

    window.history.replaceState(null, '', '/dashboard');
    rerender(<ReplayUrlGuard />);
    await vi.waitFor(() => expect(replay.startBuffering).toHaveBeenCalledTimes(1));
  });

  it('leaves replay alone on ordinary URLs', async () => {
    window.history.replaceState(null, '', '/chat?thread=t-1');
    render(<ReplayUrlGuard />);
    await Promise.resolve();
    expect(replay.stop).not.toHaveBeenCalled();
    expect(replay.startBuffering).not.toHaveBeenCalled();
  });
});
