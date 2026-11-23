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
 */

import { DEV_BACKEND_CSP_ORIGINS } from '../utils/publicEndpoints';

export interface CspEnv {
  NODE_ENV?: string;
  NEXT_PUBLIC_ANALYTICS_PROVIDER?: string;
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

export function buildCsp(
  nonce: string,
  env: CspEnv = {
    NODE_ENV: process.env.NODE_ENV,
    NEXT_PUBLIC_ANALYTICS_PROVIDER: process.env.NEXT_PUBLIC_ANALYTICS_PROVIDER,
  }
): string {
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
    // Dev: the browser calls the backend directly (getPublicApiOrigin falls
    // back to http://localhost:8000), which https:/wss: alone would block.
    `connect-src 'self' https: wss:${
      development ? ` ${DEV_BACKEND_CSP_ORIGINS.join(' ')}` : ''
    }`,
    "worker-src 'self' blob:",
    ...(development ? [] : ['upgrade-insecure-requests']),
  ].join('; ');
}
