import { API_CONFIG, APIErrorClass } from '@/types/api';

/**
 * True only for a 401 that our own backend returned (GOO-727).
 *
 * A 401 means the session is gone only when it comes from the API the
 * session authenticates against. Anything else (a 401 from a third-party
 * host, a plain Error whose message happens to say "Unauthorized", an error
 * without a recorded request URL) must never sign the user out.
 */
export function isBackendSessionFailure(
  error: unknown,
  apiBaseUrl: string = API_CONFIG.BASE_URL,
  pageOrigin: string | undefined = typeof window === 'undefined'
    ? undefined
    : window.location.origin
): boolean {
  if (!(error instanceof APIErrorClass)) return false;
  if (error.error.status_code !== 401 || !error.requestUrl) return false;
  try {
    // A relative base ('/api/v1', the local proxy) is the page's own origin.
    const backend = new URL(apiBaseUrl, pageOrigin).origin;
    return new URL(error.requestUrl, pageOrigin).origin === backend;
  } catch {
    return false;
  }
}
