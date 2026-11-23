/**
 * Browser harness for replay-privacy.spec.ts (GOO-734). Bundled at test time
 * with the real Sentry browser SDK and the app's own replay options, guard
 * and event scrubbers, so the recorded payload is what production would send.
 */
import * as Sentry from '@sentry/browser';
import {
  REPLAY_PRIVACY_OPTIONS,
  stripUrlParams,
  syncReplayWithUrl,
  type ReplayControl,
} from '../../src/lib/replay-privacy';
import {
  scrubReplayEventsIntegration,
  sentryScrubOptions,
} from '../../src/lib/sentry-scrub';

const replay = (): ReplayControl | undefined =>
  Sentry.getReplay() as ReplayControl | undefined;

const harness = {
  init(useGuard: boolean): Promise<void> {
    Sentry.init({
      dsn: 'https://public@sentry.replay.test/1',
      replaysSessionSampleRate: 0,
      replaysOnErrorSampleRate: 1,
      ...(sentryScrubOptions as object),
      integrations: [
        Sentry.replayIntegration({
          ...REPLAY_PRIVACY_OPTIONS,
          // Send soon after the error so the test is quick. Timing only:
          // what is recorded is unchanged.
          minReplayDuration: 0,
          flushMinDelay: 100,
          flushMaxDelay: 500,
        }),
        scrubReplayEventsIntegration(),
      ],
    });
    return useGuard
      ? syncReplayWithUrl(window.location.href, replay())
      : Promise.resolve();
  },
  strip(keys: string[]): void {
    stripUrlParams(keys);
  },
  sync(): Promise<void> {
    return syncReplayWithUrl(window.location.href, replay());
  },
  mode(): string | undefined {
    return replay()?.getRecordingMode();
  },
  async fail(): Promise<void> {
    Sentry.captureException(new Error('replay privacy probe'));
    await Sentry.flush(5000);
  },
};

(window as unknown as { __replayHarness: typeof harness }).__replayHarness =
  harness;
