import * as Sentry from '@sentry/nextjs';

import {
  REPLAY_PRIVACY_OPTIONS,
  installReplayUrlGuard,
  type ReplayControl,
} from './src/lib/replay-privacy';
import {
  SENTRY_CONSOLE_LOG_LEVELS,
  scrubReplayEventsIntegration,
  sentryScrubOptions,
} from './src/lib/sentry-scrub';

Sentry.init({
  dsn: process.env.NEXT_PUBLIC_SENTRY_DSN,
  environment: process.env.NEXT_PUBLIC_SENTRY_ENVIRONMENT,
  release: process.env.NEXT_PUBLIC_GIT_SHA,
  tracesSampleRate: process.env.NODE_ENV === 'production' ? 0.1 : 1.0,
  profilesSampleRate: process.env.NODE_ENV === 'production' ? 0.1 : 0,
  replaysSessionSampleRate: 0,
  replaysOnErrorSampleRate: 1.0,
  enabled: Boolean(process.env.NEXT_PUBLIC_SENTRY_DSN),
  enableLogs: true,
  // Scrub user content and credentials from events, transactions, spans,
  // breadcrumbs and logs (GOO-725).
  ...sentryScrubOptions,
  integrations: [
    // Text and media masked, link targets masked, query strings stripped
    // from custom events (GOO-725, GOO-734).
    Sentry.replayIntegration(REPLAY_PRIVACY_OPTIONS),
    // Strip query strings from the replay event's URL list.
    scrubReplayEventsIntegration(),
    Sentry.consoleLoggingIntegration({ levels: SENTRY_CONSOLE_LOG_LEVELS }),
  ],
  debug: false,
});

// Replay has already taken its first snapshot, whose Meta event records the
// full URL. If that URL carries a secret or search text, drop the buffer
// before anything can send it, and check every later history change
// (GOO-734). This must run before the app hydrates, so Next's history patch
// wraps the guard rather than the other way round.
installReplayUrlGuard(() => Sentry.getReplay() as ReplayControl | undefined);

export const onRouterTransitionStart = Sentry.captureRouterTransitionStart;
