/**
 * GOO-729: same-origin sink for CSP violation reports.
 *
 * Unauthenticated by design (browsers post reports without credentials), so
 * it never echoes input, never logs a raw body and never throws: every
 * request gets an empty 204. Reports are scrubbed to directive, disposition,
 * origins and a listed route (src/utils/cspReport.ts), deduped and capped per
 * window, then forwarded through the server Sentry SDK, where the GOO-725
 * hooks run on them too (their keys are on the scrubber's allowlist).
 * Excluded from the proxy matcher, so no session refresh or auth redirect.
 */
import * as Sentry from '@sentry/nextjs';

import {
  CSP_REPORT_MAX_FORWARDS_PER_WINDOW,
  CSP_REPORT_MAX_KEYS,
  CSP_REPORT_WINDOW_MS,
  createCspReportSink,
  isCspReportContentType,
  MAX_CSP_REPORT_BYTES,
  parseCspReports,
  type ScrubbedCspReport,
} from '@/utils/cspReport';

export const dynamic = 'force-dynamic';

function forward(report: ScrubbedCspReport, overflow: number): void {
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
      csp_batch_dropped: overflow,
    },
  });
}

function forwardCapReached(limit: number): void {
  Sentry.captureMessage('CSP report cap reached', {
    level: 'warning',
    fingerprint: ['csp-report-cap'],
    extra: { csp_window_cap: limit },
  });
}

const sink = createCspReportSink({
  windowMs: CSP_REPORT_WINDOW_MS,
  maxForwardsPerWindow: CSP_REPORT_MAX_FORWARDS_PER_WINDOW,
  maxKeys: CSP_REPORT_MAX_KEYS,
  forward,
  forwardCapReached,
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
    const { reports, overflow } = parseCspReports(
      contentType,
      body,
      new URL(request.url).origin
    );
    for (const report of reports) sink.record(report, overflow);
  } catch {
    // Never surface anything to an anonymous caller.
  }
  return noContent();
}
