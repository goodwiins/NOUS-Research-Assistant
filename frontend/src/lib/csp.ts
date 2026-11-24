/**
 * The app's Content-Security-Policy, built per request by `proxy.ts`.
 *
 * Uses only relative imports (no `@/` alias) so the Playwright CSP specs can
 * import the exact production policy instead of hand-copying it.
 *
 * img-src (GOO-722): the app never loads images from an arbitrary remote
 * origin, so `https:` is not allowed. A remote image the app did not plan
 * for, such as an attacker-chosen URL in rendered content, cannot be fetched
 * and so cannot leak data through its query string. What is allowed, and why:
 *  - 'self': static assets under /public (logos, landing and auth artwork)
 *    and next/image, which serves through same-origin /_next/image.
 *  - blob: object URLs for authenticated previews fetched by the app
 *    itself (document inline viewer, artifact image preview) and local file
 *    previews in the chat composer and upload wizard.
 *  - data: the entity-graph PNG export draws a data: SVG, and libraries
 *    inline small data: images.
 *  - Google Analytics beacon hosts, only when NEXT_PUBLIC_ANALYTICS_PROVIDER
 *    is 'google-analytics' (the GA loader in lib/analytics.ts).
 * Add a remote origin only with a real feature that needs it, and read it
 * from configuration rather than hard-coding one environment's host.
 *
 * connect-src (GOO-729): the enforced policy still allows bare `https:` and
 * `wss:`. The explicit list below ships first as a separate
 * Content-Security-Policy-Report-Only header (connectSrcReportOnly), reported
 * to the same-origin CSP_REPORT_PATH route, so a missed origin shows up as a report
 * instead of breaking login, chat streaming or uploads. Once reports are
 * clean, a follow-up moves the list into the enforced policy. Every origin
 * comes from public config or from the request's own host:
 *  - 'self': Next routes (/api/trigger/*, the /monitoring Sentry tunnel,
 *    the local /api/v1 proxy) and same-origin WebSockets.
 *  - the backend API and its WebSocket twin: NEXT_PUBLIC_API_URL /
 *    NEXT_PUBLIC_API_BASE_URL, else derived from the page host exactly as the
 *    browser does (resolveApiOrigin). Uploads go through the API.
 *  - the WebSocket origin: NEXT_PUBLIC_WS_URL / NEXT_PUBLIC_WEBSOCKET_URL.
 *  - Supabase auth: NEXT_PUBLIC_SUPABASE_URL (REST only; no realtime use).
 *  - Sentry ingest: the NEXT_PUBLIC_SENTRY_DSN host, in case an event
 *    bypasses the tunnel.
 *  - analytics, per NEXT_PUBLIC_ANALYTICS_PROVIDER: Google Analytics hosts,
 *    plausible.io, or the NEXT_PUBLIC_ANALYTICS_ENDPOINT origin.
 *  - Statsig, only when NEXT_PUBLIC_STATSIG_CLIENT_KEY is set: the SDK's
 *    fixed hosts (@statsig/client-core 3.33).
 */

import {
  DEV_BACKEND_CSP_ORIGINS,
  resolveApiOrigin,
  resolveWebSocketOrigin,
  type PublicEndpointEnv,
} from '../utils/publicEndpoints';

export interface CspEnv extends PublicEndpointEnv {
  NODE_ENV?: string;
  NEXT_PUBLIC_ANALYTICS_PROVIDER?: string;
  NEXT_PUBLIC_ANALYTICS_ENDPOINT?: string;
  NEXT_PUBLIC_SUPABASE_URL?: string;
  NEXT_PUBLIC_SENTRY_DSN?: string;
  NEXT_PUBLIC_STATSIG_CLIENT_KEY?: string;
}

/** The CSP-relevant public config, read where it is inlined at build. */
export function processCspEnv(): CspEnv {
  return {
    NODE_ENV: process.env.NODE_ENV,
    NEXT_PUBLIC_ANALYTICS_PROVIDER: process.env.NEXT_PUBLIC_ANALYTICS_PROVIDER,
    NEXT_PUBLIC_ANALYTICS_ENDPOINT: process.env.NEXT_PUBLIC_ANALYTICS_ENDPOINT,
    NEXT_PUBLIC_SUPABASE_URL: process.env.NEXT_PUBLIC_SUPABASE_URL,
    NEXT_PUBLIC_SENTRY_DSN: process.env.NEXT_PUBLIC_SENTRY_DSN,
    NEXT_PUBLIC_STATSIG_CLIENT_KEY: process.env.NEXT_PUBLIC_STATSIG_CLIENT_KEY,
    NEXT_PUBLIC_API_URL: process.env.NEXT_PUBLIC_API_URL,
    NEXT_PUBLIC_API_BASE_URL: process.env.NEXT_PUBLIC_API_BASE_URL,
    NEXT_PUBLIC_WS_URL: process.env.NEXT_PUBLIC_WS_URL,
    NEXT_PUBLIC_WEBSOCKET_URL: process.env.NEXT_PUBLIC_WEBSOCKET_URL,
  };
}

/** Hosts gtag.js may send image beacons to (Google's documented CSP list). */
export const GOOGLE_ANALYTICS_IMG_ORIGINS = [
  'https://*.google-analytics.com',
  'https://*.googletagmanager.com',
] as const;

export function imgSrc(env: CspEnv): string {
  const sources = ["'self'", 'data:', 'blob:'];
  if (env.NEXT_PUBLIC_ANALYTICS_PROVIDER === 'google-analytics') {
    sources.push(...GOOGLE_ANALYTICS_IMG_ORIGINS);
  }
  return `img-src ${sources.join(' ')}`;
}

/** Google's documented connect-src hosts for gtag.js. */
export const GOOGLE_ANALYTICS_CONNECT_ORIGINS = [
  'https://*.google-analytics.com',
  'https://*.analytics.google.com',
  'https://*.googletagmanager.com',
] as const;

/**
 * Hosts the Statsig browser SDK (@statsig/client-core 3.33: initialize,
 * event logging, CDN fallback, exception reporting, and its DNS-over-HTTPS
 * fallback-domain lookup) connects to. They are the vendor's, the same in
 * every environment.
 */
// The hosts @statsig/client-core 3.33.8 has in its source. Its network
// fallback domains are not static: it reads them from DNS TXT records of
// featureassets.org through cloudflare-dns.com at runtime, so they cannot be
// listed here; under Report-Only they show up as reports (GOO-739).
export const STATSIG_CONNECT_ORIGINS = [
  'https://featureassets.org',
  'https://prodregistryv2.org',
  'https://api.statsigcdn.com',
  'https://statsigapi.net',
  'https://cloudflare-dns.com',
] as const;

/** `scheme://host[:port]` of an absolute URL with an allowed scheme. */
function originOf(
  value: string | undefined,
  protocols: readonly string[]
): string | null {
  const trimmed = value?.trim();
  if (!trimmed) return null;
  try {
    const url = new URL(trimmed);
    return protocols.includes(url.protocol) ? url.origin : null;
  } catch {
    return null; // relative ('/api/v1') or malformed: same-origin or unused
  }
}

const HTTP = ['http:', 'https:'] as const;
const WS = ['ws:', 'wss:'] as const;

/**
 * The origins the browser connects to (see the header comment), for a page
 * served at `requestUrl`.
 */
export function connectSources(env: CspEnv, requestUrl: URL | null): string[] {
  const sources = new Set<string>(["'self'"]);
  const add = (origin: string | null): void => {
    if (origin) sources.add(origin);
  };

  const api = originOf(resolveApiOrigin(env, requestUrl), HTTP);
  add(api);
  // Configured WS origin, else the API origin with a ws scheme.
  add(originOf(resolveWebSocketOrigin(env, requestUrl), WS));
  add(originOf(env.NEXT_PUBLIC_SUPABASE_URL, HTTP));
  add(originOf(env.NEXT_PUBLIC_SENTRY_DSN, HTTP));

  switch (env.NEXT_PUBLIC_ANALYTICS_PROVIDER) {
    case 'google-analytics':
      GOOGLE_ANALYTICS_CONNECT_ORIGINS.forEach(add);
      break;
    case 'plausible':
      add('https://plausible.io');
      break;
    case 'custom':
      add(originOf(env.NEXT_PUBLIC_ANALYTICS_ENDPOINT, HTTP));
      break;
  }
  if (env.NEXT_PUBLIC_STATSIG_CLIENT_KEY?.trim()) {
    STATSIG_CONNECT_ORIGINS.forEach(add);
  }
  if (env.NODE_ENV === 'development') DEV_BACKEND_CSP_ORIGINS.forEach(add);
  return [...sources];
}

export const CSP_REPORT_GROUP = 'csp-endpoint';

/**
 * Same-origin CSP report sink (app/api/csp-report). Reports never go
 * straight from the browser to a third party: a raw report carries the page
 * URL and the blocked URL with their query strings (search text, auth codes,
 * token_hash, cli-auth session ids). The route keeps only the directive, the
 * disposition and origins, then forwards through the server Sentry SDK.
 */
export const CSP_REPORT_PATH = '/api/csp-report';

/**
 * The Report-Only policy: the explicit connect-src list alone, so only
 * connections are evaluated, plus the same-origin report endpoint.
 */
export function connectSrcReportOnly(
  env: CspEnv = processCspEnv(),
  requestUrl: URL | null = null
): string {
  return [
    `connect-src ${connectSources(env, requestUrl).join(' ')}`,
    `report-uri ${CSP_REPORT_PATH}`,
    `report-to ${CSP_REPORT_GROUP}`,
  ].join('; ');
}

/**
 * `Reporting-Endpoints` header value for report-to: the report route on the
 * request's own origin (absolute, as the Reporting API expects).
 */
export function reportingEndpoints(requestUrl: URL): string {
  return `${CSP_REPORT_GROUP}="${new URL(CSP_REPORT_PATH, requestUrl.origin).toString()}"`;
}

export function buildCsp(nonce: string, env: CspEnv = processCspEnv()): string {
  const development = env.NODE_ENV === 'development';
  return [
    "default-src 'self'",
    "base-uri 'self'",
    "object-src 'none'",
    "frame-src 'self' blob:",
    "frame-ancestors 'none'",
    "form-action 'self'",
    // React dev mode needs eval() for debugging features (callstack
    // reconstruction); never emitted in production builds.
    `script-src 'self' 'nonce-${nonce}' 'strict-dynamic'${
      development ? " 'unsafe-eval'" : ''
    }`,
    "style-src 'self' 'unsafe-inline'",
    imgSrc(env),
    "font-src 'self' data:",
    // Still enforced as bare schemes; the explicit list is report-only for
    // now (connectSrcReportOnly, GOO-729). Dev: the browser calls the backend
    // directly (getPublicApiOrigin falls back to http://localhost:8000),
    // which https:/wss: alone would block.
    `connect-src 'self' https: wss:${
      development ? ` ${DEV_BACKEND_CSP_ORIGINS.join(' ')}` : ''
    }`,
    "worker-src 'self' blob:",
    ...(development ? [] : ['upgrade-insecure-requests']),
  ].join('; ');
}
