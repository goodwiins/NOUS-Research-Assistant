/**
 * GOO-729: scrubbing for browser CSP violation reports.
 *
 * The browser posts reports to the same-origin /api/csp-report route (see
 * CSP_REPORT_PATH in csp.ts). A raw report holds the page URL, the blocked
 * URL, the referrer and a script sample, and page URLs here can carry search
 * text, the verify-email token_hash, the auth callback code and the cli-auth
 * session_id. This module keeps only:
 *  - the directive (`connect-src`) and the disposition (`report`/`enforce`),
 *  - the ORIGIN of the blocked URL (or a CSP keyword such as `inline`),
 *  - the ORIGIN of the page plus its first path segment when that segment is
 *    a plain lowercase route name (`/chat`, `/verify-email`), never ids.
 * Everything else (query strings, fragments, paths, referrer, script-sample,
 * source-file, line numbers, status codes) is dropped.
 *
 * Every function here is total: malformed input yields no report, never an
 * exception, and nothing from the input is logged or echoed.
 */

export const CSP_REPORT_CONTENT_TYPES = [
  'application/csp-report',
  'application/reports+json',
] as const;

/** Bodies larger than this are dropped unread. A real report is < 2 KB. */
export const MAX_CSP_REPORT_BYTES = 4096;

/** Forward 10% of first-seen reports, at most once per key per window. */
export const CSP_REPORT_SAMPLE_RATE = 0.1;
export const CSP_REPORT_DEDUPE_MS = 10 * 60 * 1000;
export const CSP_REPORT_MAX_KEYS = 500;

/** A reports+json batch is capped; the rest is ignored. */
export const MAX_REPORTS_PER_REQUEST = 5;

export interface ScrubbedCspReport {
  directive: string;
  disposition: 'enforce' | 'report' | 'unknown';
  blocked: string;
  documentOrigin: string;
  documentRoute: string;
}

type AnyRecord = Record<string, unknown>;

const isRecord = (value: unknown): value is AnyRecord =>
  typeof value === 'object' && value !== null && !Array.isArray(value);

const DIRECTIVE = /^[a-z][a-z-]{0,39}$/;
const ROUTE_SEGMENT = /^[a-z][a-z-]{0,31}$/;
const URL_PROTOCOLS = new Set(['http:', 'https:', 'ws:', 'wss:']);
// Non-URL values CSP puts in blocked-uri / blockedURL.
const BLOCKED_KEYWORDS = new Set([
  'inline',
  'eval',
  'wasm-eval',
  'data',
  'blob',
  'self',
  'trusted-types-policy',
  'trusted-types-sink',
]);

/** `text/x; charset=…` -> `text/x`, lower-cased. */
export function baseContentType(header: string | null): string {
  return (header ?? '').split(';')[0].trim().toLowerCase();
}

export function isCspReportContentType(header: string | null): boolean {
  return (CSP_REPORT_CONTENT_TYPES as readonly string[]).includes(
    baseContentType(header)
  );
}

function scrubDirective(value: unknown): string | null {
  if (typeof value !== 'string') return null;
  // Legacy violated-directive may be the whole directive: "connect-src 'self' …".
  const name = value.trim().split(/\s+/)[0]?.toLowerCase() ?? '';
  return DIRECTIVE.test(name) ? name : null;
}

function scrubDisposition(value: unknown): ScrubbedCspReport['disposition'] {
  return value === 'enforce' || value === 'report' ? value : 'unknown';
}

/** A URL's origin, a CSP keyword, or 'other'. Never a path or query. */
export function scrubBlocked(value: unknown): string {
  if (typeof value !== 'string' || value.length === 0) return 'other';
  const keyword = value.trim().toLowerCase().replace(/:$/, '');
  if (BLOCKED_KEYWORDS.has(keyword)) return keyword;
  try {
    const url = new URL(value);
    return URL_PROTOCOLS.has(url.protocol) ? url.origin : 'other';
  } catch {
    return 'other';
  }
}

/** The page's origin and a coarse route: `/` + first segment, or `/`. */
export function scrubDocument(value: unknown): {
  origin: string;
  route: string;
} {
  if (typeof value !== 'string') return { origin: 'other', route: '/' };
  try {
    const url = new URL(value);
    if (!URL_PROTOCOLS.has(url.protocol))
      return { origin: 'other', route: '/' };
    const first = url.pathname.split('/')[1] ?? '';
    return {
      origin: url.origin,
      route: ROUTE_SEGMENT.test(first) ? `/${first}` : '/',
    };
  } catch {
    return { origin: 'other', route: '/' };
  }
}

function scrubOne(
  directiveValue: unknown,
  disposition: unknown,
  blocked: unknown,
  documentUrl: unknown
): ScrubbedCspReport | null {
  const directive = scrubDirective(directiveValue);
  if (!directive) return null;
  const document = scrubDocument(documentUrl);
  return {
    directive,
    disposition: scrubDisposition(disposition),
    blocked: scrubBlocked(blocked),
    documentOrigin: document.origin,
    documentRoute: document.route,
  };
}

/**
 * Parse and scrub a report body. Returns [] for a wrong content type, an
 * oversize body, malformed JSON or an unexpected shape.
 */
export function parseCspReports(
  contentType: string | null,
  body: string
): ScrubbedCspReport[] {
  try {
    const type = baseContentType(contentType);
    if (!isCspReportContentType(type)) return [];
    if (new TextEncoder().encode(body).length > MAX_CSP_REPORT_BYTES) return [];
    const parsed: unknown = JSON.parse(body);

    if (type === 'application/csp-report') {
      // report-uri: { "csp-report": { "effective-directive", … } }
      const report = isRecord(parsed) ? parsed['csp-report'] : undefined;
      if (!isRecord(report)) return [];
      const one = scrubOne(
        report['effective-directive'] ?? report['violated-directive'],
        report.disposition,
        report['blocked-uri'],
        report['document-uri']
      );
      return one ? [one] : [];
    }

    // report-to: [{ type: "csp-violation", body: { effectiveDirective, … } }]
    if (!Array.isArray(parsed)) return [];
    const out: ScrubbedCspReport[] = [];
    for (const entry of parsed.slice(0, MAX_REPORTS_PER_REQUEST)) {
      if (!isRecord(entry) || entry.type !== 'csp-violation') continue;
      const reportBody = entry.body;
      if (!isRecord(reportBody)) continue;
      const one = scrubOne(
        reportBody.effectiveDirective,
        reportBody.disposition,
        reportBody.blockedURL,
        reportBody.documentURL
      );
      if (one) out.push(one);
    }
    return out;
  } catch {
    return [];
  }
}

export interface CspReportSinkOptions {
  /** Fraction of first-seen reports forwarded (0..1). */
  sampleRate: number;
  /** Window in which a repeat of the same key is dropped. */
  dedupeMs: number;
  /** Keys remembered at most; the map is reset past this. */
  maxKeys: number;
  forward: (report: ScrubbedCspReport) => void;
  now?: () => number;
  random?: () => number;
}

export interface CspReportSink {
  record: (report: ScrubbedCspReport) => boolean;
}

/**
 * Dedupe per directive + blocked origin + page origin inside a window, then
 * sample. Per-instance memory: on serverless each instance dedupes on its
 * own, which is enough to stop one broken page from flooding.
 */
export function createCspReportSink(
  options: CspReportSinkOptions
): CspReportSink {
  const now = options.now ?? Date.now;
  const random = options.random ?? Math.random;
  const lastSeen = new Map<string, number>();
  return {
    record(report: ScrubbedCspReport): boolean {
      const key = `${report.directive}|${report.blocked}|${report.documentOrigin}`;
      const time = now();
      const previous = lastSeen.get(key);
      if (previous !== undefined && time - previous < options.dedupeMs) {
        return false;
      }
      if (lastSeen.size >= options.maxKeys) lastSeen.clear();
      lastSeen.set(key, time);
      if (random() >= options.sampleRate) return false;
      try {
        options.forward(report);
      } catch {
        return false;
      }
      return true;
    },
  };
}
