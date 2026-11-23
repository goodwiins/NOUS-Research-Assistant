/**
 * GOO-729: same-origin sink for CSP violation reports.
 *
 * Unauthenticated by design (browsers post reports without credentials), so
 * it never echoes input, never logs a raw body and never throws: every
 * request gets an empty 204. Reports are scrubbed to directive, disposition
 * and origins (src/lib/cspReport.ts), deduped and sampled, then forwarded
 * through the server Sentry SDK so the GOO-725 hooks also apply.
 * Excluded from the proxy matcher, so no session refresh or auth redirect.
 */
import * as Sentry from '@sentry/nextjs';

import {
  CSP_REPORT_DEDUPE_MS,
  CSP_REPORT_MAX_KEYS,
  CSP_REPORT_SAMPLE_RATE,
  createCspReportSink,
  isCspReportContentType,
  MAX_CSP_REPORT_BYTES,
  parseCspReports,
  type ScrubbedCspReport,
} from '@/lib/cspReport';

export const dynamic = 'force-dynamic';

function forward(report: ScrubbedCspReport): void {
  Sentry.captureMessage('CSP violation', {
    level: 'warning',
    fingerprint: ['csp-violation', report.directive, report.blocked],
    tags: {
      csp_directive: report.directive,
      csp_disposition: report.disposition,
      csp_blocked: report.blocked,
    },
    extra: {
      document_origin: report.documentOrigin,
      document_route: report.documentRoute,
    },
  });
}

const sink = createCspReportSink({
  sampleRate: CSP_REPORT_SAMPLE_RATE,
  dedupeMs: CSP_REPORT_DEDUPE_MS,
  maxKeys: CSP_REPORT_MAX_KEYS,
  forward,
});

const noContent = (): Response => new Response(null, { status: 204 });

/** Read at most MAX_CSP_REPORT_BYTES; null when the body is larger. */
async function readCapped(request: Request): Promise<string | null> {
  const declared = Number(request.headers.get('content-length'));
  if (Number.isFinite(declared) && declared > MAX_CSP_REPORT_BYTES) return null;
  if (!request.body) return '';
  const reader = request.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    size += value.byteLength;
    if (size > MAX_CSP_REPORT_BYTES) {
      await reader.cancel().catch(() => undefined);
      return null;
    }
    chunks.push(value);
  }
  const bytes = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return new TextDecoder().decode(bytes);
}

export async function POST(request: Request): Promise<Response> {
  try {
    const contentType = request.headers.get('content-type');
    if (!isCspReportContentType(contentType)) return noContent();
    const body = await readCapped(request);
    if (body === null) return noContent();
    for (const report of parseCspReports(contentType, body))
      sink.record(report);
  } catch {
    // Never surface anything to an anonymous caller.
  }
  return noContent();
}
