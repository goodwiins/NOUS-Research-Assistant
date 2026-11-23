/**
 * Scrubbing hooks shared by every Sentry.init (client, server, edge) (GOO-725).
 *
 * Without hooks, console output, error events, traces and replay metadata
 * left the app as is, so any console call or URL that carried user content
 * (tool args/results, message or prompt text, thread titles, document text,
 * search queries, tokens) reached Sentry. These hooks run on everything
 * before it is sent:
 *
 * - Structure over patterns. Structured data (event extra, contexts, tags,
 *   breadcrumb data, span data, log attributes) keeps the string value of a
 *   key only when the key is on an allowlist of technical fields (ids,
 *   status, method, counts, durations, ...). Other string values become
 *   `[Filtered]`; numbers and booleans survive unless the key is on the
 *   credential/content denylist. Unknown top-level event fields are dropped.
 * - Console arguments that are objects or arrays are dropped.
 * - Strings are first cut to a few KB, then redacted with bounded patterns
 *   (JWTs incl. URL-encoded and partial ones, bearer/Authorization and
 *   Cookie values, API keys, AWS keys, Supabase keys, emails, sensitive
 *   `name=value` params, SSE `data:` payloads, inline JSON, URL query
 *   strings), then truncated. Every quantifier is bounded, so the cost is
 *   linear in the (already capped) input.
 * - Request cookies, bodies, query strings, env and auth headers, stack
 *   frame locals and source context are removed; the user is reduced to id.
 * - Every hook fails closed: if scrubbing throws, breadcrumbs, logs,
 *   transactions and replay frames are dropped, and an error event is
 *   replaced by a minimal one (type, release, frames without vars/source).
 *
 * Limitation: free text inside a plain string (a prompt or a title in an
 * error message) is not detected. It is bounded by truncation (200 chars,
 * 300 for error messages), not recognised. Keep user content out of console
 * calls and error messages in the first place.
 *
 * Pure functions with no SDK imports beyond types, so they are unit-tested
 * directly and safe to load in the edge runtime.
 */
import type {
  Breadcrumb,
  BreadcrumbHint,
  ErrorEvent,
  EventHint,
  Log,
  init,
} from '@sentry/nextjs';

// Not re-exported by @sentry/nextjs. Derived from init's options rather than
// imported from @sentry/core, which is only a transitive dependency and is
// not resolvable under the image's isolated pnpm layout.
type SentryInitOptions = NonNullable<Parameters<typeof init>[0]>;
type SpanJSON = Parameters<NonNullable<SentryInitOptions['beforeSendSpan']>>[0];
type TransactionEvent = Parameters<
  NonNullable<SentryInitOptions['beforeSendTransaction']>
>[0];

export const MAX_STRING_LENGTH = 200;
const MAX_ERROR_MESSAGE_LENGTH = 300;
/** Strings are cut to this length before any pattern runs. */
export const MAX_SCAN_LENGTH = 4096;
const MAX_DEPTH = 4;
const MAX_ARRAY_ITEMS = 20;

export const FILTERED = '[Filtered]';
export const OBJECT_PLACEHOLDER = '[Object]';

type AnyRecord = Record<string, unknown>;

const isRecord = (value: unknown): value is AnyRecord =>
  typeof value === 'object' && value !== null && !Array.isArray(value);

// ---------------------------------------------------------------------------
// Keys
// ---------------------------------------------------------------------------

/** Lower-case and drop `_`, `-`, `.` and spaces: toolArgs == tool_args. */
const normalizeKey = (key: string): string =>
  key.toLowerCase().replace(/[\s_.-]/g, '');

// Values under these keys are content or credentials: always filtered,
// even when they are numbers.
const SENSITIVE_KEYS = new Set([
  'args', 'arguments', 'toolargs', 'toolarguments', 'toolinput', 'input',
  'result', 'results', 'toolresult', 'tooloutput', 'output',
  'content', 'contents', 'message', 'messages', 'text', 'prompt',
  'systemprompt', 'query', 'q', 'title', 'threadtitle', 'document',
  'documenttext', 'body', 'requestbody', 'responsebody', 'payload', 'delta',
  'chunk', 'name', 'description', 'summary', 'snippet', 'filename',
  'question', 'answer', 'value', 'data', 'response', 'preview', 'variables',
  'headers', 'username', 'ip', 'ipaddress', 'phone', 'key', 'credentials',
  'accesskey', 'cookie', 'cookies', 'setcookie', 'authorization',
  'proxyauthorization', 'auth', 'password', 'secret', 'apikey', 'xapikey',
  'jwt', 'session', 'email', 'httpquery', 'urlquery', 'httpfragment',
  'querystring', 'env',
]);

// Suffixes that mark credentials or personal data regardless of prefix
// (accessToken, supabaseToken, userEmail, clientSecret, ...).
const SENSITIVE_SUFFIX =
  /(token|secret|password|cookie|authorization|email|apikey|jwt|credentials)$/;

export function isSensitiveKey(key: string): boolean {
  const normalized = normalizeKey(key);
  return SENSITIVE_KEYS.has(normalized) || SENSITIVE_SUFFIX.test(normalized);
}

// Technical fields whose string values are kept (still pattern-scrubbed).
const SAFE_KEYS = new Set([
  'status', 'statuscode', 'statustext', 'httpstatuscode',
  'httpresponsestatuscode', 'httprequestmethod', 'httpmethod', 'method',
  'code', 'errorcode', 'errortype', 'type', 'kind', 'level', 'category', 'op',
  'origin', 'count', 'total', 'length', 'size', 'bytes', 'duration',
  'durationms', 'elapsedms', 'latencyms', 'timeout', 'attempt', 'attempts',
  'retry', 'retries', 'step', 'index', 'page', 'pagesize', 'limit', 'offset',
  'phase', 'stage', 'state', 'version', 'environment', 'release', 'logger',
  'component', 'componentstack', 'operation', 'route', 'transaction',
  'source', 'handled', 'synthetic', 'readystate', 'serveraddress',
  'serverport', 'networkprotocolname', 'networkprotocolversion', 'urlpath',
  'framework', 'runtime', 'browser', 'os', 'device', 'sampled', 'mechanism',
]);

// Keys whose values are URLs: kept without their query string or fragment.
const URL_KEYS = new Set([
  'url', 'httpurl', 'urlfull', 'href', 'from', 'to', 'referer', 'referrer',
  'httptarget', 'target',
]);

// thread_id, span_id, threadId, ids, ...
const ID_KEY = /(?:^|[_.\-\s])ids?$|[a-z0-9]Ids?$|^id$/i;

type KeyClass = 'sdk' | 'url' | 'deny' | 'safe' | 'unknown';

function classifyKey(key: string): KeyClass {
  if (key.startsWith('sentry.')) return 'sdk';
  const normalized = normalizeKey(key);
  if (URL_KEYS.has(normalized)) return 'url';
  if (isSensitiveKey(key)) return 'deny';
  if (SAFE_KEYS.has(normalized) || ID_KEY.test(key)) return 'safe';
  return 'unknown';
}

// ---------------------------------------------------------------------------
// Strings
// ---------------------------------------------------------------------------

const REDACTIONS: Array<[RegExp, string]> = [
  // SSE frames: the payload after `data:` is user/tool content. Matches a
  // line that starts with `data:`, or `data:` followed by JSON anywhere;
  // plain prose such as "Invalid data: x" is left alone.
  [/(^|\n)[ \t]{0,16}data:[^\n]{0,4096}/g, '$1data: [Filtered]'],
  [/\bdata:[ \t]{0,4}[{[][^\n]{0,4096}/g, 'data: [Filtered]'],
  // Inline JSON objects/arrays: everything from `{"` / `["` to end of line.
  [/[{[][ \t]{0,4}"[^\n]{0,4096}/g, OBJECT_PLACEHOLDER],
  // Authorization / Cookie header values, with or without a scheme.
  [/\b(?:proxy-)?authorization\b[ \t]{0,4}[:=][ \t]{0,4}[^\n,;]{1,1024}/gi, 'Authorization: [Filtered]'],
  [/\b(?:set-)?cookie\b[ \t]{0,4}[:=][ \t]{0,4}[^\n]{1,4096}/gi, 'Cookie: [Filtered]'],
  [/\bsb-[A-Za-z0-9-]{1,128}=[^;\s"']{1,4096}/g, 'sb-[cookie]=[Filtered]'],
  [/\b(Bearer|Basic|Token)\s{1,4}[A-Za-z0-9._~+/=-]{8,4096}/gi, '$1 [Filtered]'],
  // JSON Web Tokens: full (dots raw or URL-encoded), then any fragment left
  // by truncation or an encoding the first pattern does not know.
  [/eyJ[A-Za-z0-9_-]{2,4096}(?:\.|%2[Ee])[A-Za-z0-9_-]{2,4096}(?:\.|%2[Ee])[A-Za-z0-9_-]{0,4096}/g, '[jwt]'],
  [/eyJ[A-Za-z0-9_-]{8,4096}/g, '[jwt]'],
  // Prefixed API keys and cloud credentials.
  [/\b(?:sk|pk|rk|ghp|gho|ghs|ghu|xox[abprs])[-_][A-Za-z0-9_-]{12,256}/g, '[key]'],
  [/\bsb_(?:secret|publishable)_[A-Za-z0-9_-]{8,256}/g, '[key]'],
  [/\bsbp_[A-Za-z0-9]{16,256}/g, '[key]'],
  [/\b(?:AKIA|ASIA)[A-Z0-9]{16}\b/g, '[key]'],
  // Sensitive name=value pairs anywhere (bare or in a query string).
  [/\b(access_token|refresh_token|id_token|token|api_?key|apikey|secret|password|passwd|signature|jwt|session_?id)=[^&\s"'<>#]{1,2048}/gi, '$1=[Filtered]'],
  // Email addresses (bounded local part and domain: linear, not quadratic).
  [/[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,24}/g, '[email]'],
  // Query strings and fragments: absolute URLs, then relative paths.
  [/(https?:\/\/[^\s?#"'<>]{1,2048})[?#][^\s"'<>]{0,4096}/g, '$1?[Filtered]'],
  [/(^|[\s"'(=])(\/[^\s?#"'<>]{0,512})[?#][^\s"'<>]{1,4096}/g, '$1$2?[Filtered]'],
];

export function scrubString(value: string, maxLength = MAX_STRING_LENGTH): string {
  const originalLength = value.length;
  let out = value;
  if (out.length > MAX_SCAN_LENGTH) {
    // Cut BEFORE any pattern runs, and drop the trailing partial token so a
    // secret split by the cut cannot survive as an unrecognised fragment.
    out = out.slice(0, MAX_SCAN_LENGTH).replace(/\S{0,256}$/, '');
  }
  for (const [pattern, replacement] of REDACTIONS) {
    out = out.replace(pattern, replacement);
  }
  if (out.length > maxLength || originalLength > MAX_SCAN_LENGTH) {
    const kept = out.slice(0, maxLength);
    out = `${kept}…[truncated ${Math.max(0, originalLength - kept.length)} chars]`;
  }
  return out;
}

/** Remove the query string and fragment from a URL or path. */
export function stripQuery(url: string): string {
  const head = url.length > MAX_SCAN_LENGTH ? url.slice(0, MAX_SCAN_LENGTH) : url;
  const cut = head.search(/[?#]/);
  return cut === -1 ? head : head.slice(0, cut);
}

const scrubUrl = (value: unknown): unknown =>
  typeof value === 'string' ? scrubString(stripQuery(value)) : value;

// ---------------------------------------------------------------------------
// Values
// ---------------------------------------------------------------------------

const isErrorLike = (value: unknown): value is { name?: unknown; message?: unknown } =>
  value instanceof Error ||
  (isRecord(value) &&
    typeof value.stack === 'string' &&
    typeof value.message === 'string');

/**
 * One console argument (or log parameter): primitives survive (strings
 * scrubbed), errors keep only `Name: message`, everything else is dropped.
 */
export function scrubArgument(value: unknown): unknown {
  if (value === null || value === undefined) return value;
  if (typeof value === 'string') return scrubString(value);
  if (typeof value === 'number' || typeof value === 'boolean') return value;
  if (typeof value === 'bigint') return value.toString();
  if (isErrorLike(value)) {
    const name = typeof value.name === 'string' ? value.name : 'Error';
    const message = typeof value.message === 'string' ? value.message : '';
    return scrubString(`${name}: ${message}`, MAX_ERROR_MESSAGE_LENGTH);
  }
  if (Array.isArray(value)) return `[Array(${value.length})]`;
  return OBJECT_PLACEHOLDER;
}

/** A value under `key` inside structured data (allowlist rules). */
function scrubKeyed(key: string, value: unknown, depth: number): unknown {
  const kind = classifyKey(key);
  if (kind === 'deny') return FILTERED;
  if (kind === 'url' && typeof value === 'string') return scrubUrl(value);
  if (value === null || value === undefined) return value;
  if (typeof value === 'number' || typeof value === 'boolean') return value;
  if (typeof value === 'bigint') return value.toString();
  if (typeof value === 'string') {
    return kind === 'unknown' ? FILTERED : scrubString(value);
  }
  if (isErrorLike(value)) return scrubArgument(value);
  return scrubNested(value, depth, kind);
}

function scrubNested(value: unknown, depth: number, kind: KeyClass): unknown {
  if (typeof value !== 'object' || value === null) {
    return kind === 'unknown' ? FILTERED : value;
  }
  if (depth >= MAX_DEPTH) return OBJECT_PLACEHOLDER;
  if (Array.isArray(value)) {
    const items = value.slice(0, MAX_ARRAY_ITEMS).map((item: unknown) => {
      if (typeof item === 'string') return kind === 'unknown' ? FILTERED : scrubString(item);
      if (typeof item === 'object' && item !== null) return scrubNested(item, depth + 1, kind);
      return item;
    });
    if (value.length > MAX_ARRAY_ITEMS) items.push(`[${value.length - MAX_ARRAY_ITEMS} more]`);
    return items;
  }
  return scrubData(value as AnyRecord, depth + 1);
}

/** Structured data: allowlisted keys keep their (scrubbed) values. */
export function scrubData(data: AnyRecord, depth = 0): AnyRecord {
  const out: AnyRecord = {};
  for (const key of Object.keys(data)) {
    try {
      out[key] = scrubKeyed(key, data[key], depth);
    } catch {
      out[key] = FILTERED; // throwing getter / Proxy trap
    }
  }
  return out;
}

const looksSerialized = (message: string): boolean => /^\s{0,16}[{[]/.test(message);

/** A formatted console message: dropped if it is a serialized object. */
function scrubConsoleMessage(message: string): string {
  return looksSerialized(message) ? OBJECT_PLACEHOLDER : scrubString(message);
}

const asText = (value: unknown): string =>
  typeof value === 'string' ? value : String(value);

// ---------------------------------------------------------------------------
// Logs (consoleLoggingIntegration + Sentry.logger)
// ---------------------------------------------------------------------------

const TEMPLATE_KEY = 'sentry.message.template';
const PARAMETER_PREFIX = 'sentry.message.parameter.';
const MAX_PARAMETERS = 50;

export function scrubLog(log: Log): Log {
  const attributes = isRecord(log.attributes) ? log.attributes : {};
  const scrubbedAttributes: AnyRecord = {};
  const parameters: string[] = [];

  for (const key of Object.keys(attributes)) {
    const value = attributes[key];
    if (key.startsWith(PARAMETER_PREFIX)) {
      const index = Number(key.slice(PARAMETER_PREFIX.length));
      const scrubbed = scrubArgument(value);
      scrubbedAttributes[key] = scrubbed;
      if (Number.isInteger(index) && index >= 0 && index < MAX_PARAMETERS) {
        parameters[index] = asText(scrubbed);
      }
    } else if (key === TEMPLATE_KEY) {
      scrubbedAttributes[key] = typeof value === 'string' ? scrubString(value) : FILTERED;
    } else if (key.startsWith('sentry.')) {
      // SDK-owned metadata (origin, sdk name/version, environment).
      scrubbedAttributes[key] = typeof value === 'string' ? scrubString(value) : value;
    } else {
      // Keys spread from a plain-object first console argument.
      scrubbedAttributes[key] = scrubKeyed(key, value, 1);
    }
  }

  let message: string;
  const template = attributes[TEMPLATE_KEY];
  if (typeof template === 'string') {
    // Rebuild "first {} {}" from the scrubbed parameters, so an object
    // argument never reaches the formatted message.
    let index = 0;
    message = scrubString(
      template
        .slice(0, MAX_SCAN_LENGTH)
        .replace(/\{\}/g, () => parameters[index++] ?? FILTERED)
    );
  } else {
    message = scrubConsoleMessage(asText(log.message));
  }

  return { ...log, message, attributes: scrubbedAttributes };
}

// ---------------------------------------------------------------------------
// Breadcrumbs
// ---------------------------------------------------------------------------

const BREADCRUMB_KEYS = ['type', 'category', 'level', 'timestamp', 'event_id'] as const;

export function scrubBreadcrumb(breadcrumb: Breadcrumb, _hint?: BreadcrumbHint): Breadcrumb {
  const source = breadcrumb as unknown as AnyRecord;
  const out: AnyRecord = {};
  for (const key of BREADCRUMB_KEYS) {
    if (source[key] !== undefined) out[key] = source[key];
  }
  const isConsole = source.category === 'console';

  if (typeof source.message === 'string') {
    out.message = isConsole ? scrubConsoleMessage(source.message) : scrubString(source.message);
  }

  if (isRecord(source.data)) {
    if (isConsole) {
      const args = source.data.arguments;
      out.data = {
        logger: typeof source.data.logger === 'string' ? source.data.logger : undefined,
        arguments: Array.isArray(args)
          ? args.slice(0, MAX_ARRAY_ITEMS).map(scrubArgument)
          : undefined,
      };
    } else {
      out.data = scrubData(source.data, 1);
    }
  }

  return out as Breadcrumb;
}

// ---------------------------------------------------------------------------
// Error and transaction events
// ---------------------------------------------------------------------------

const SAFE_HEADERS = new Set([
  'accept', 'accept-language', 'content-type', 'content-length', 'host',
  'referer', 'user-agent',
]);

// Fields an event keeps as they are; every other top-level field is either
// handled below or dropped.
const EVENT_PASSTHROUGH = [
  'event_id', 'timestamp', 'start_timestamp', 'level', 'platform', 'logger',
  'release', 'dist', 'environment', 'sdk', 'fingerprint', 'type',
  'transaction_info', 'measurements', 'debug_meta', 'modules',
  'sdkProcessingMetadata',
] as const;

// Frame fields kept; vars, context_line, pre_context and post_context hold
// locals and source lines, so they go.
const FRAME_KEYS = [
  'function', 'module', 'lineno', 'colno', 'in_app', 'platform',
  'instruction_addr', 'addr_mode', 'symbol', 'symbol_addr', 'image_addr',
  'package', 'debug_id',
] as const;

function scrubFrames(frames: unknown): AnyRecord[] | undefined {
  if (!Array.isArray(frames)) return undefined;
  return frames.filter(isRecord).map((frame) => {
    const out: AnyRecord = {};
    for (const key of FRAME_KEYS) {
      if (frame[key] !== undefined) out[key] = frame[key];
    }
    if (typeof frame.filename === 'string') out.filename = stripQuery(frame.filename);
    if (typeof frame.abs_path === 'string') out.abs_path = stripQuery(frame.abs_path);
    return out;
  });
}

const scrubStacktrace = (stacktrace: unknown): AnyRecord | undefined =>
  isRecord(stacktrace) ? { frames: scrubFrames(stacktrace.frames) } : undefined;

// `data` (and `meta`) can hold arbitrary values, so they are not kept.
const MECHANISM_KEYS = [
  'type', 'handled', 'synthetic', 'source', 'is_exception_group',
  'exception_id', 'parent_id',
] as const;

function scrubException(exception: AnyRecord): AnyRecord {
  const out: AnyRecord = {};
  if (typeof exception.type === 'string') out.type = scrubString(exception.type);
  if (typeof exception.value === 'string') {
    out.value = scrubString(exception.value, MAX_ERROR_MESSAGE_LENGTH);
  }
  if (typeof exception.module === 'string') out.module = exception.module;
  if (exception.thread_id !== undefined) out.thread_id = exception.thread_id;
  if (isRecord(exception.mechanism)) {
    const mechanism: AnyRecord = {};
    for (const key of MECHANISM_KEYS) {
      if (exception.mechanism[key] !== undefined) mechanism[key] = exception.mechanism[key];
    }
    out.mechanism = mechanism;
  }
  const stacktrace = scrubStacktrace(exception.stacktrace);
  if (stacktrace) out.stacktrace = stacktrace;
  return out;
}

function scrubRequest(request: AnyRecord): AnyRecord {
  // cookies, data, query_string and env are dropped.
  const out: AnyRecord = {};
  if (typeof request.url === 'string') out.url = scrubString(stripQuery(request.url));
  if (typeof request.method === 'string') out.method = request.method;
  if (isRecord(request.headers)) {
    const headers: AnyRecord = {};
    for (const name of Object.keys(request.headers)) {
      const value = request.headers[name];
      const lower = name.toLowerCase();
      if (!SAFE_HEADERS.has(lower) || typeof value !== 'string') continue;
      headers[name] = lower === 'referer' ? scrubString(stripQuery(value)) : scrubString(value);
    }
    out.headers = headers;
  }
  return out;
}

// SDK-filled environment contexts: technical, kept with strings scrubbed.
const ENVIRONMENT_CONTEXTS = new Set([
  'browser', 'os', 'device', 'runtime', 'app', 'culture', 'cloud_resource',
  'react',
]);
const TRACE_CONTEXT_KEYS = [
  'trace_id', 'span_id', 'parent_span_id', 'op', 'status', 'origin', 'sampled',
] as const;

function scrubContexts(contexts: AnyRecord): AnyRecord {
  const out: AnyRecord = {};
  for (const name of Object.keys(contexts)) {
    const context = contexts[name];
    if (!isRecord(context)) continue;
    if (name === 'trace') {
      const trace: AnyRecord = {};
      for (const key of TRACE_CONTEXT_KEYS) {
        if (context[key] !== undefined) trace[key] = context[key];
      }
      if (isRecord(context.data)) trace.data = scrubData(context.data, 1);
      out[name] = trace;
    } else if (ENVIRONMENT_CONTEXTS.has(name)) {
      const env: AnyRecord = {};
      for (const key of Object.keys(context)) {
        const value = context[key];
        if (typeof value === 'string') env[key] = scrubString(value);
        else if (typeof value === 'number' || typeof value === 'boolean') env[key] = value;
      }
      out[name] = env;
    } else {
      out[name] = scrubData(context, 1);
    }
  }
  return out;
}

function scrubLogentry(logentry: AnyRecord): AnyRecord {
  const out: AnyRecord = {};
  if (typeof logentry.message === 'string') {
    out.message = scrubString(logentry.message, MAX_ERROR_MESSAGE_LENGTH);
  }
  if (Array.isArray(logentry.params)) {
    // Template parameters are the caller's values: only numbers/booleans.
    out.params = logentry.params
      .slice(0, MAX_ARRAY_ITEMS)
      .map((param: unknown) =>
        typeof param === 'number' || typeof param === 'boolean' ? param : FILTERED
      );
  }
  return out;
}

const SPAN_LINK_KEYS = ['span_id', 'trace_id', 'sampled'] as const;

export function scrubSpan(span: SpanJSON): SpanJSON {
  const source = span as unknown as AnyRecord;
  const out: AnyRecord = {
    ...source,
    data: isRecord(source.data) ? scrubData(source.data, 1) : {},
  };
  if (typeof source.description === 'string') out.description = scrubString(source.description);
  if (Array.isArray(source.links)) {
    out.links = source.links.filter(isRecord).map((link) => {
      const kept: AnyRecord = {};
      for (const key of SPAN_LINK_KEYS) {
        if (link[key] !== undefined) kept[key] = link[key];
      }
      return kept;
    });
  }
  return out as unknown as SpanJSON;
}

/** Shared by error and transaction events. */
function scrubEventFields(event: AnyRecord): AnyRecord {
  const out: AnyRecord = {};
  for (const key of EVENT_PASSTHROUGH) {
    if (event[key] !== undefined) out[key] = event[key];
  }
  if (typeof event.message === 'string') {
    out.message = scrubString(event.message, MAX_ERROR_MESSAGE_LENGTH);
  }
  if (typeof event.transaction === 'string') {
    out.transaction = scrubString(stripQuery(event.transaction));
  }
  if (isRecord(event.logentry)) out.logentry = scrubLogentry(event.logentry);
  if (isRecord(event.exception) && Array.isArray(event.exception.values)) {
    out.exception = { values: event.exception.values.filter(isRecord).map(scrubException) };
  }
  if (isRecord(event.threads) && Array.isArray(event.threads.values)) {
    out.threads = {
      values: event.threads.values.filter(isRecord).map((thread) => ({
        id: thread.id,
        crashed: thread.crashed,
        current: thread.current,
        main: thread.main,
        stacktrace: scrubStacktrace(thread.stacktrace),
      })),
    };
  }
  if (isRecord(event.request)) out.request = scrubRequest(event.request);
  if (isRecord(event.user)) {
    out.user = event.user.id !== undefined ? { id: event.user.id } : {};
  }
  if (isRecord(event.contexts)) out.contexts = scrubContexts(event.contexts);
  if (isRecord(event.tags)) out.tags = scrubData(event.tags, 1);
  if (isRecord(event.extra)) out.extra = scrubData(event.extra);
  if (Array.isArray(event.breadcrumbs)) {
    out.breadcrumbs = event.breadcrumbs.filter(isRecord).flatMap((crumb) => {
      try {
        return [scrubBreadcrumb(crumb as Breadcrumb)];
      } catch {
        return [];
      }
    });
  }
  if (Array.isArray(event.spans)) {
    out.spans = event.spans
      .filter(isRecord)
      .map((span) => scrubSpan(span as unknown as SpanJSON));
  }
  return out;
}

export function scrubEvent(event: ErrorEvent, _hint?: EventHint): ErrorEvent {
  return scrubEventFields(event as unknown as AnyRecord) as unknown as ErrorEvent;
}

export function scrubTransaction(
  event: TransactionEvent,
  _hint?: EventHint
): TransactionEvent {
  return scrubEventFields(event as unknown as AnyRecord) as unknown as TransactionEvent;
}

const MINIMAL_EVENT_KEYS = [
  'event_id', 'timestamp', 'level', 'platform', 'release', 'dist',
  'environment',
] as const;

/**
 * Fail-closed replacement for an error event whose scrubbing threw: the
 * exception type, release and frames (no vars, no source), nothing else.
 */
export function minimalEvent(event: ErrorEvent): ErrorEvent | null {
  try {
    const source = event as unknown as AnyRecord;
    const out: AnyRecord = { tags: { scrub_failed: true } };
    for (const key of MINIMAL_EVENT_KEYS) {
      const value = source[key];
      if (typeof value === 'string' || typeof value === 'number') out[key] = value;
    }
    const values =
      isRecord(source.exception) && Array.isArray(source.exception.values)
        ? source.exception.values
        : [];
    out.exception = {
      values: values.filter(isRecord).map((exception) => {
        let frames: AnyRecord[] | undefined;
        try {
          frames = scrubFrames(
            isRecord(exception.stacktrace) ? exception.stacktrace.frames : undefined
          );
        } catch {
          frames = undefined;
        }
        return {
          type: typeof exception.type === 'string' ? exception.type.slice(0, 100) : 'Error',
          ...(frames ? { stacktrace: { frames } } : {}),
        };
      }),
    };
    return out as unknown as ErrorEvent;
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------------------
// Replay
// ---------------------------------------------------------------------------

function scrubReplayBreadcrumbFrame(frame: AnyRecord): AnyRecord {
  if (frame.category === 'console') {
    return { ...frame, ...(scrubBreadcrumb(frame as Breadcrumb) as AnyRecord) };
  }
  const out: AnyRecord = { ...frame };
  if (typeof frame.message === 'string') {
    out.message =
      frame.category === 'navigation' ? stripQuery(frame.message) : scrubString(frame.message);
  }
  if (isRecord(frame.data)) {
    const data: AnyRecord = { ...frame.data };
    for (const key of Object.keys(data)) {
      if (URL_KEYS.has(normalizeKey(key))) data[key] = scrubUrl(data[key]);
    }
    out.data = data;
  }
  return out;
}

function scrubReplaySpanFrame(frame: AnyRecord): AnyRecord {
  // navigation.*, resource.* and request frames use the URL as description.
  const out: AnyRecord = { ...frame };
  if (typeof frame.description === 'string') out.description = scrubUrl(frame.description);
  if (isRecord(frame.data)) {
    const data: AnyRecord = {};
    for (const key of Object.keys(frame.data)) {
      const value = frame.data[key];
      if (URL_KEYS.has(normalizeKey(key))) data[key] = scrubUrl(value);
      else if (key === 'request' || key === 'response') {
        // Network detail: keep only sizes, never headers or bodies.
        data[key] = isRecord(value) ? { size: value.size } : undefined;
      } else data[key] = typeof value === 'string' ? scrubString(value) : value;
    }
    out.data = data;
  }
  return out;
}

/**
 * replayIntegration({ beforeAddRecordingEvent }): strips query strings from
 * navigation breadcrumbs and from resource/request span names and data, and
 * scrubs console frames. maskAllText/blockAllMedia still cover the DOM.
 */
export function scrubReplayRecordingEvent<T extends { data?: unknown }>(event: T): T | null {
  try {
    const data = event.data;
    if (!isRecord(data) || !isRecord(data.payload)) return event;
    if (data.tag === 'breadcrumb') {
      return { ...event, data: { ...data, payload: scrubReplayBreadcrumbFrame(data.payload) } };
    }
    if (data.tag === 'performanceSpan') {
      return { ...event, data: { ...data, payload: scrubReplaySpanFrame(data.payload) } };
    }
    return event;
  } catch {
    return null;
  }
}

/**
 * Strips query strings from the replay event's URL list (`urls`), which no
 * before* hook sees. Integration processEvent runs for replay events too.
 */
export interface ScrubReplayEventsIntegration {
  name: string;
  processEvent<E extends { type?: unknown }>(event: E): E;
}

export function scrubReplayEventsIntegration(): ScrubReplayEventsIntegration {
  return {
    name: 'ScrubReplayEvents',
    processEvent<E extends { type?: unknown }>(event: E): E {
      if (event.type !== 'replay_event') return event;
      const record = event as unknown as AnyRecord;
      try {
        const out: AnyRecord = { ...record };
        if (Array.isArray(record.urls)) {
          out.urls = record.urls.map((url: unknown) =>
            typeof url === 'string' ? stripQuery(url) : url
          );
        }
        if (isRecord(record.request)) out.request = scrubRequest(record.request);
        return out as unknown as E;
      } catch {
        const out: AnyRecord = { ...record, urls: [] };
        delete out.request;
        return out as unknown as E;
      }
    },
  };
}

// ---------------------------------------------------------------------------
// Fail-closed hooks
// ---------------------------------------------------------------------------

export function safeBeforeSend(event: ErrorEvent, hint?: EventHint): ErrorEvent | null {
  try {
    return scrubEvent(event, hint);
  } catch {
    return minimalEvent(event);
  }
}

export function safeBeforeSendTransaction(
  event: TransactionEvent,
  hint?: EventHint
): TransactionEvent | null {
  try {
    return scrubTransaction(event, hint);
  } catch {
    return null;
  }
}

export function safeBeforeSendSpan(span: SpanJSON): SpanJSON {
  try {
    return scrubSpan(span);
  } catch {
    // The hook may not drop a span, so send it without description or data.
    return {
      span_id: span.span_id,
      trace_id: span.trace_id,
      start_timestamp: span.start_timestamp,
      timestamp: span.timestamp,
      op: span.op,
      status: span.status,
      data: {},
    };
  }
}

export function safeBeforeBreadcrumb(
  breadcrumb: Breadcrumb,
  hint?: BreadcrumbHint
): Breadcrumb | null {
  try {
    return scrubBreadcrumb(breadcrumb, hint);
  } catch {
    return null;
  }
}

export function safeBeforeSendLog(log: Log): Log | null {
  try {
    return scrubLog(log);
  } catch {
    return null;
  }
}

/** Spread into every Sentry.init so the hooks cannot drift apart. */
export const sentryScrubOptions = {
  beforeSend: safeBeforeSend,
  beforeSendTransaction: safeBeforeSendTransaction,
  beforeSendSpan: safeBeforeSendSpan,
  beforeBreadcrumb: safeBeforeBreadcrumb,
  beforeSendLog: safeBeforeSendLog,
};

/**
 * Console levels forwarded to Sentry Logs. Error only: warn and log are
 * mostly debug output from the chat and upload paths, which is where user
 * content has leaked before; errors are the level worth keeping.
 */
export const SENTRY_CONSOLE_LOG_LEVELS: Array<'error'> = ['error'];
