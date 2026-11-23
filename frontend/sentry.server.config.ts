import * as Sentry from '@sentry/nextjs';

import { SENTRY_CONSOLE_LOG_LEVELS, sentryScrubOptions } from './src/lib/sentry-scrub';

Sentry.init({
  dsn: process.env.NEXT_PUBLIC_SENTRY_DSN,
  environment: process.env.NEXT_PUBLIC_SENTRY_ENVIRONMENT,
  release: process.env.NEXT_PUBLIC_GIT_SHA,
  tracesSampleRate: process.env.NODE_ENV === 'production' ? 0.1 : 1.0,
  profilesSampleRate: process.env.NODE_ENV === 'production' ? 0.1 : 0,
  enabled: Boolean(process.env.NEXT_PUBLIC_SENTRY_DSN),
  enableLogs: true,
  // Scrub user content and credentials from events, breadcrumbs and logs (GOO-725).
  ...sentryScrubOptions,
  integrations: [
    Sentry.consoleLoggingIntegration({ levels: SENTRY_CONSOLE_LOG_LEVELS }),
  ],
  debug: false,
});
