/**
 * GOO-729: scrubbing and rate limiting for browser CSP violation reports.
 *
 * The browser posts reports to the same-origin /api/csp-report route (see
 * CSP_REPORT_PATH in lib/csp.ts). A raw report holds the page URL, the
 * blocked URL, the referrer and a script sample, and page URLs here can carry
 * search text, the verify-email token_hash, the auth callback code and the
 * cli-auth session_id. This module keeps only:
 *  - the directive, when it is a known CSP directive name,
 *  - the disposition (`report` / `enforce`),
 *  - the ORIGIN of the blocked URL (or a CSP keyword such as `inline`),
 *  - the page's first path segment when it is one of the app's top-level
 *    routes (CSP_REPORT_ROUTES), else `/`.
 * A report whose page origin is not the request's own origin is dropped.
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

/** A reports+json batch is capped; the overflow is counted, not parsed. */
export const MAX_REPORTS_PER_REQUEST = 5;

/** Dedupe window, and the window the forward cap applies to. */
export const CSP_REPORT_WINDOW_MS = 10 * 60 * 1000;
/** Events forwarded per instance per window before a single cap event. */
export const CSP_REPORT_MAX_FORWARDS_PER_WINDOW = 50;
/** Dedupe keys remembered; the oldest are evicted first. */
export const CSP_REPORT_MAX_KEYS = 500;

/** CSP directive names a violation report can carry (CSP Level 3). */
export const KNOWN_CSP_DIRECTIVES: ReadonlySet<string> = new Set([
  'base-uri',
  'child-src',
  'connect-src',
  'default-src',
  'fenced-frame-src',
  'font-src',
  'form-action',
  'frame-ancestors',
  'frame-src',
  'img-src',
  'manifest-src',
  'media-src',
  'object-src',
  'require-trusted-types-for',
  'script-src',
  'script-src-attr',
  'script-src-elem',
  'style-src',
  'style-src-attr',
  'style-src-elem',
  'trusted-types',
  'worker-src',
]);

/**
 * The app's top-level page routes (first URL segment of every page under
 * app/, route groups flattened). A test keeps this equal to app/, so a new
 * top-level route, or a dynamic one, fails until it is listed here.
 */
export const CSP_REPORT_ROUTES: ReadonlySet<string> = new Set([
  'agent-plan-demo',
  'analytics',
  'artifacts',
  'arxiv',
  'auth',
  'chat',
  'cli-auth',
  'dashboard',
  'design-system',
  'diagnostics',
  'documents',
  'entities',
  'forgot-password',
  'integrations',
  'login',
  'notifications',
  'projects',
  'quality-metrics-demo',
  'register',
  'research',
  'research-engine',
  'reset-password',
  'search',
  'settings',
  'verify-email',
  'visual-test',
]);

export interface ScrubbedCspReport {
  directive: string;
  disposition: 'enforce' | 'report' | 'unknown';
  blocked: string;
  documentOrigin: string;
  documentRoute: string;
}

export interface ParsedCspReports {
  reports: ScrubbedCspReport[];
  /** Batch entries past MAX_REPORTS_PER_REQUEST, not parsed. */
  overflow: number;
}

type AnyRecord = Record<string, unknown>;

const NONE: ParsedCspReports = { reports: [], overflow: 0 };

const isRecord = (value: unknown): value is AnyRecord =>
  typeof value === 'object' && value !== null && !Array.isArray(value);

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
  return KNOWN_CSP_DIRECTIVES.has(name) ? name : null;
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

/** The page's origin and its listed top-level route, or `/`. */
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
      route: CSP_REPORT_ROUTES.has(first) ? `/${first}` : '/',
    };
  } catch {
    return { origin: 'other', route: '/' };
  }
}

function scrubOne(
  directiveValue: unknown,
  disposition: unknown,
  blocked: unknown,
  documentUrl: unknown,
  requestOrigin: string
): ScrubbedCspReport | null {
  const directive = scrubDirective(directiveValue);
  if (!directive) return null;
  const document = scrubDocument(documentUrl);
  // Only our own pages: a forged page origin is dropped, not forwarded.
  if (document.origin !== requestOrigin) return null;
  return {
    directive,
    disposition: scrubDisposition(disposition),
    blocked: scrubBlocked(blocked),
    documentOrigin: document.origin,
    documentRoute: document.route,
  };
}

/**
 * Parse and scrub a report body posted to `requestOrigin`. Returns no
 * reports for a wrong content type, an oversize body, malformed JSON or an
 * unexpected shape.
 */
export function parseCspReports(
  contentType: string | null,
  body: string,
  requestOrigin: string
): ParsedCspReports {
  try {
    const type = baseContentType(contentType);
    if (!isCspReportContentType(type)) return NONE;
    if (new TextEncoder().encode(body).length > MAX_CSP_REPORT_BYTES)
      return NONE;
    const parsed: unknown = JSON.parse(body);

    if (type === 'application/csp-report') {
      // report-uri: { "csp-report": { "effective-directive", … } }
      const report = isRecord(parsed) ? parsed['csp-report'] : undefined;
      if (!isRecord(report)) return NONE;
      const one = scrubOne(
        report['effective-directive'] ?? report['violated-directive'],
        report.disposition,
        report['blocked-uri'],
        report['document-uri'],
        requestOrigin
      );
      return { reports: one ? [one] : [], overflow: 0 };
    }

    // report-to: [{ type: "csp-violation", body: { effectiveDirective, … } }]
    if (!Array.isArray(parsed)) return NONE;
    const reports: ScrubbedCspReport[] = [];
    for (const entry of parsed.slice(0, MAX_REPORTS_PER_REQUEST)) {
      if (!isRecord(entry) || entry.type !== 'csp-violation') continue;
      const reportBody = entry.body;
      if (!isRecord(reportBody)) continue;
      const one = scrubOne(
        reportBody.effectiveDirective,
        reportBody.disposition,
        reportBody.blockedURL,
        reportBody.documentURL,
        requestOrigin
      );
      if (one) reports.push(one);
    }
    return {
      reports,
      overflow: Math.max(0, parsed.length - MAX_REPORTS_PER_REQUEST),
    };
  } catch {
    return NONE;
  }
}

export interface CspReportSinkOptions {
  /** Dedupe window, and the window the forward cap counts in. */
  windowMs: number;
  /** Forwarded events per window; then one cap event, then nothing. */
  maxForwardsPerWindow: number;
  /** Dedupe keys remembered; the oldest are evicted first. */
  maxKeys: number;
  forward: (report: ScrubbedCspReport, overflow: number) => void;
  forwardCapReached: (limit: number) => void;
  now?: () => number;
}

export type CspReportOutcome = 'forwarded' | 'duplicate' | 'capped' | 'failed';

export interface CspReportSink {
  record: (report: ScrubbedCspReport, overflow?: number) => CspReportOutcome;
}

/**
 * No sampling: every first-seen key is forwarded, so a rare missed origin is
 * never lost. Volume is bounded by the per-key dedupe and a per-window cap.
 * A key is remembered only once its event was forwarded, so a capped or
 * failed report is tried again later. Per-instance memory: on serverless
 * each instance limits on its own.
 */
export function createCspReportSink(
  options: CspReportSinkOptions
): CspReportSink {
  const now = options.now ?? Date.now;
  // Insertion-ordered: the first entry is the oldest forward.
  const lastForwarded = new Map<string, number>();
  let windowStart = Number.NEGATIVE_INFINITY;
  let forwardedInWindow = 0;
  let capReported = false;

  return {
    record(report: ScrubbedCspReport, overflow = 0): CspReportOutcome {
      const time = now();
      if (time - windowStart >= options.windowMs) {
        windowStart = time;
        forwardedInWindow = 0;
        capReported = false;
      }
      const key = `${report.directive}|${report.blocked}|${report.documentOrigin}`;
      const previous = lastForwarded.get(key);
      if (previous !== undefined && time - previous < options.windowMs) {
        return 'duplicate';
      }
      if (forwardedInWindow >= options.maxForwardsPerWindow) {
        if (!capReported) {
          capReported = true;
          try {
            options.forwardCapReached(options.maxForwardsPerWindow);
          } catch {
            // The cap event is best effort.
          }
        }
        return 'capped';
      }
      try {
        options.forward(report, overflow);
      } catch {
        return 'failed';
      }
      forwardedInWindow += 1;
      lastForwarded.delete(key);
      lastForwarded.set(key, time);
      while (lastForwarded.size > options.maxKeys) {
        const oldest = lastForwarded.keys().next().value;
        if (oldest === undefined) break;
        lastForwarded.delete(oldest);
      }
      return 'forwarded';
    },
  };
}
