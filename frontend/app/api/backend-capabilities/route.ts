import { resolveBackendUrl } from '../../../config/resolveBackendUrl';
import type { ApiBackendCapabilities } from '../../../src/types/api/backend-capabilities-contract';

const unavailable = { draftClaims: false, draftRelease: false };

/** Read the deployed API, which may be older than this frontend build. */
export async function GET(): Promise<Response> {
  const headers = { 'Cache-Control': 'no-store' };
  try {
    const response = await fetch(
      `${resolveBackendUrl()}/api/v1/backend-capabilities`,
      {
        cache: 'no-store',
        signal: AbortSignal.timeout(3000),
      }
    );
    if (!response.ok) throw new Error('Capability discovery unavailable');
    const payload: unknown = await response.json();
    if (
      !payload ||
      typeof payload !== 'object' ||
      !('draftClaims' in payload) ||
      typeof payload.draftClaims !== 'boolean' ||
      !('draftRelease' in payload) ||
      typeof payload.draftRelease !== 'boolean'
    ) {
      throw new Error('Invalid capability response');
    }
    const capabilities: ApiBackendCapabilities = {
      draftClaims: payload.draftClaims,
      draftRelease: payload.draftRelease,
    };
    return Response.json(capabilities, { headers });
  } catch {
    return Response.json(unavailable, { status: 503, headers });
  }
}
