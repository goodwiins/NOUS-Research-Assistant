// @vitest-environment node
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { GET } from '../../../app/api/backend-capabilities/route';

describe('deployed backend capabilities', () => {
  const fetchMock = vi.fn();
  beforeEach(() => {
    vi.stubEnv('BACKEND_URL', 'https://server.example.com/api/v1');
    vi.stubGlobal('fetch', fetchMock);
    fetchMock.mockReset();
  });
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
  });
  it('reads the deployed backend feature probe', async () => {
    fetchMock.mockResolvedValue(
      Response.json({ draftClaims: true, draftRelease: true })
    );
    const response = await GET();
    expect(await response.json()).toEqual({
      draftClaims: true,
      draftRelease: true,
    });
    expect(fetchMock).toHaveBeenCalledWith(
      'https://server.example.com/api/v1/backend-capabilities',
      expect.objectContaining({ cache: 'no-store' })
    );
    expect(response.headers.get('Cache-Control')).toContain('no-store');
  });
  it('disables controls on an older backend', async () => {
    fetchMock.mockResolvedValue(new Response(null, { status: 404 }));
    expect((await GET()).status).toBe(503);
    expect(await (await GET()).json()).toEqual({
      draftClaims: false,
      draftRelease: false,
    });
  });
  it('keeps unavailable features disabled', async () => {
    fetchMock.mockResolvedValue(
      Response.json({ draftClaims: true, draftRelease: false })
    );
    expect(await (await GET()).json()).toEqual({
      draftClaims: true,
      draftRelease: false,
    });
  });
  it.each([null, {}, { draftClaims: true, draftRelease: 'true' }])(
    'fails closed on malformed capability response %s',
    async (payload) => {
      fetchMock.mockResolvedValue(Response.json(payload));
      expect((await GET()).status).toBe(503);
    }
  );
  it('fails closed when the feature probe fails', async () => {
    fetchMock.mockRejectedValue(new Error('internal connection information'));
    const response = await GET();
    expect(response.status).toBe(503);
    expect(await response.json()).toEqual({
      draftClaims: false,
      draftRelease: false,
    });
  });
});
