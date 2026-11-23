// @vitest-environment node
/**
 * GOO-729 B1: the route's event must survive the GOO-725 scrubber readable.
 * No mock of Sentry here: the real server SDK, initialised with the real
 * sentryScrubOptions hooks, sends through a capturing transport, and the
 * test reads what would leave the process.
 */
import * as Sentry from '@sentry/nextjs';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';

import { sentryScrubOptions } from '@/lib/sentry-scrub';

type Envelope = [unknown, Array<[{ type?: string }, unknown]>];
const envelopes: Envelope[] = [];

function sentEvents(): Record<string, unknown>[] {
  return envelopes.flatMap(([, items]) =>
    items
      .filter(([header]) => header.type === 'event')
      .map(([, payload]) => payload as Record<string, unknown>)
  );
}

beforeAll(() => {
  Sentry.init({
    dsn: 'https://public@o1.ingest.sentry.example/42',
    ...sentryScrubOptions,
    defaultIntegrations: false,
    transport: () => ({
      send: async (envelope: unknown) => {
        envelopes.push(envelope as Envelope);
        return { statusCode: 200 };
      },
      flush: async () => true,
    }),
  });
});

afterAll(async () => {
  await Sentry.close();
});

const PAGE = 'https://dev-app.one.test';

function report(blocked: string, document: string): Request {
  return new Request(`${PAGE}/api/csp-report`, {
    method: 'POST',
    headers: { 'content-type': 'application/csp-report' },
    body: JSON.stringify({
      'csp-report': {
        'document-uri': document,
        referrer: `${PAGE}/search?q=my%20private%20search`,
        'effective-directive': 'connect-src',
        disposition: 'report',
        'blocked-uri': blocked,
        'script-sample': 'alert(document.cookie)',
        'source-file': `${PAGE}/_next/app.js?token=eyJhbGciOiJIUzI1NiJ9.e30.x`,
      },
    }),
  });
}

describe('POST /api/csp-report through the real Sentry hooks', () => {
  it('arrives with every field readable and nothing sensitive', async () => {
    const { POST } = await import('../../app/api/csp-report/route');
    const response = await POST(
      report(
        'https://exfil.example/c?session_id=cli-session-77&token=t0k3n',
        `${PAGE}/verify-email/4f1c9e2a-1111-4222-8333-944445555666?token_hash=th-secret&code=pkce-code-123#frag`
      )
    );
    expect(response.status).toBe(204);
    await Sentry.flush(2000);

    const events = sentEvents();
    expect(events).toHaveLength(1);
    const [event] = events;
    expect(event.message).toBe('CSP violation');
    expect(event.tags).toMatchObject({
      csp_directive: 'connect-src',
      csp_disposition: 'report',
      csp_blocked: 'https://exfil.example',
    });
    expect(event.extra).toMatchObject({
      document_origin: PAGE,
      document_route: '/verify-email',
      csp_batch_dropped: 0,
    });

    const text = JSON.stringify(event);
    expect(text).not.toContain('[Filtered]');
    for (const secret of [
      'cli-session-77',
      't0k3n',
      'th-secret',
      'pkce-code-123',
      '4f1c9e2a',
      'private search',
      'document.cookie',
      'eyJhbGci',
      'token_hash',
      'session_id',
    ]) {
      expect(text).not.toContain(secret);
    }
    expect(text).not.toMatch(/https?:\/\/[^"]*\?/);
  });
});
