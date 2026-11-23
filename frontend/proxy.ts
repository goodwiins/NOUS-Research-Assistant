import { type NextRequest, type NextResponse } from 'next/server';
import { updateSession } from '@/lib/supabase/middleware';
import { AUTH_RETURN_TO_HEADER } from '@/utils/authRedirect';
import {
  buildCsp,
  connectSrcReportOnly,
  processCspEnv,
  reportingEndpoints,
} from '@/lib/csp';

/**
 * Per-request CSP with a script nonce (audit #4 completion).
 *
 * The static CSP added in #695 had to allow 'unsafe-inline'/'unsafe-eval' in
 * script-src because there was no per-request nonce. Generating the nonce here
 * and propagating it on the REQUEST headers (via updateSession →
 * NextResponse.next({ request: { headers } })) lets Next 16 apply it to its
 * own framework/hydration scripts, so script-src can drop unsafe-inline
 * entirely. 'strict-dynamic' lets nonce-trusted scripts load their chunks.
 * style-src keeps 'unsafe-inline' (styled-jsx/Tailwind inline styles — a style
 * nonce is a separate follow-up). The static CSP was removed from
 * next.config.js: two CSP headers enforce their intersection.
 *
 * The policy itself lives in src/lib/csp.ts (GOO-722) so tests and the
 * Playwright CSP specs use the exact production policy.
 */

// NOTE (AU8, 2026-07-16): this proxy intentionally does NOT gate auth. It
// only refreshes the Supabase session cookie (updateSession) and sets the
// CSP nonce — no getUser()-checked redirect for unauthenticated requests.
// Route protection is enforced server-side per route tree: app/(dashboard)/
// layout.tsx guards all dashboard pages with getUser() + redirect('/login'),
// and app/api/trigger/** routes each do their own getUser() -> 401. A new
// top-level protected route MUST bring its own guard (or live under an
// existing guarded layout) — there is no middleware backstop here. See the
// comment on app/(dashboard)/layout.tsx for the full history of why an
// earlier comment implied otherwise.
export async function proxy(request: NextRequest): Promise<NextResponse> {
  const nonce = Buffer.from(crypto.randomUUID()).toString('base64');
  const csp = buildCsp(nonce);

  // Mutated request headers must flow to the renderer for Next to pick up the
  // nonce — updateSession passes them via NextResponse.next({request:{headers}}).
  const requestHeaders = new Headers(request.headers);
  requestHeaders.set('x-nonce', nonce);
  requestHeaders.set('Content-Security-Policy', csp);
  // The dashboard server layout cannot otherwise recover the concrete URL
  // that entered a route group. Always overwrite the inbound value so a
  // client cannot spoof an external/post-login destination.
  requestHeaders.set(
    AUTH_RETURN_TO_HEADER,
    `${request.nextUrl.pathname}${request.nextUrl.search}`
  );

  const response = await updateSession(request, requestHeaders);

  // And the browser needs the policy on the response.
  response.headers.set('Content-Security-Policy', csp);

  // GOO-729: the explicit connect-src list, observed before it is enforced.
  // Built from public config plus this request's host, which is how the
  // browser derives the API origin when NEXT_PUBLIC_API_URL is unset.
  // Reports go to the same-origin /api/csp-report route, which scrubs them.
  const requestUrl = new URL(request.url);
  response.headers.set(
    'Content-Security-Policy-Report-Only',
    connectSrcReportOnly(processCspEnv(), requestUrl)
  );
  response.headers.set('Reporting-Endpoints', reportingEndpoints(requestUrl));

  return response;
}

export const config = {
  matcher: [
    /*
     * Match all request paths except:
     * - _next/static (static files)
     * - _next/image (image optimization)
     * - favicon.ico (favicon)
     * - public assets
     * - api/csp-report: anonymous browser reports; no session refresh, no
     *   auth redirect, no CSP headers on the 204 (GOO-729)
     */
    '/((?!_next/static|_next/image|favicon.ico|api/csp-report$|.*\\.(?:svg|png|jpg|jpeg|gif|webp)$).*)',
  ],
};
