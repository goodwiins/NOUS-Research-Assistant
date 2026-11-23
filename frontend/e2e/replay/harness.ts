/**
 * Browser harness for replay-privacy.spec.ts (GOO-734). Bundled at test time
 * with the real Sentry browser SDK and the app's own replay options, guard
 * and event scrubbers, so the recorded payload is what production would send.
 */
import * as Sentry from '@sentry/browser';
import {
  REPLAY_PRIVACY_OPTIONS,
  installReplayUrlGuard,
  stripUrlParams,
  type ReplayControl,
} from '../../src/lib/replay-privacy';
import {
  scrubReplayEventsIntegration,
  sentryScrubOptions,
} from '../../src/lib/sentry-scrub';

const replay = (): ReplayControl | undefined =>
  Sentry.getReplay() as ReplayControl | undefined;

let probes = 0;

const harness = {
  init(useGuard: boolean): void {
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
    // As in instrumentation-client: the guard checks the current URL and
    // every later history change.
    if (useGuard) installReplayUrlGuard(replay);
  },
  strip(keys: string[]): void {
    stripUrlParams(keys);
  },
  navigate(path: string): void {
    window.history.pushState(null, '', path);
  },
  mode(): string | undefined {
    return replay()?.getRecordingMode();
  },
  async fail(): Promise<void> {
    // A unique message each time: Sentry's Dedupe drops a repeated error.
    probes += 1;
    Sentry.captureException(new Error(`replay privacy probe ${probes}`));
    await Sentry.flush(5000);
  },
};

(window as unknown as { __replayHarness: typeof harness }).__replayHarness =
  harness;
