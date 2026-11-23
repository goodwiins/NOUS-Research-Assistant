/**
 * Scrubbing hooks shared by every Sentry.init (client, server, edge).
 *
 * The configs forward console output to Sentry as logs and breadcrumbs. With
 * no hooks, any console call that included user content (tool args/results,
 * message or prompt text, thread titles, document text, tokens) was sent as
 * is (GOO-725). These hooks run on everything before it leaves the process:
 *
 * - Object and array arguments are dropped (replaced by a placeholder); only
 *   primitives and Error name/message survive.
 * - Strings are redacted (JWTs, bearer tokens, API keys, emails, SSE `data:`
 *   lines, URL query strings) and truncated.
 * - Known sensitive keys are filtered wherever structured data is kept.
 * - Request cookies, bodies, query strings and auth headers are removed, and
 *   the user is reduced to its id.
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
} from '@sentry/nextjs';

export const MAX_STRING_LENGTH = 200;
const MAX_ERROR_MESSAGE_LENGTH = 300;
const MAX_DEPTH = 4;
const MAX_ARRAY_ITEMS = 20;

export const FILTERED = '[Filtered]';
export const OBJECT_PLACEHOLDER = '[Object]';

// Keys whose values are user content or credentials, compared after
// lower-casing and removing `_`, `-` and spaces (so toolArgs, tool_args and
// tool-args all match).
const SENSITIVE_KEYS = new Set([
  'args',
  'arguments',
  'toolargs',
  'toolarguments',
  'toolinput',
  'input',
  'result',
  'results',
  'toolresult',
  'tooloutput',
  'output',
  'content',
  'contents',
  'message',
  'messages',
  'text',
  'prompt',
  'systemprompt',
  'query',
  'q',
  'title',
  'threadtitle',
  'document',
  'documenttext',
  'body',
  'requestbody',
  'responsebody',
  'payload',
  'delta',
  'chunk',
  'cookie',
  'cookies',
  'setcookie',
  'authorization',
  'proxyauthorization',
  'auth',
  'password',
  'secret',
  'apikey',
  'xapikey',
  'jwt',
  'session',
  'email',
]);

// Suffixes that mark credentials or personal data regardless of prefix
// (accessToken, supabaseToken, userEmail, clientSecret, ...).
const SENSITIVE_SUFFIX = /(token|secret|password|cookie|authorization|email|apikey|jwt)$/;

const normalizeKey = (key: string): string =>
  key.toLowerCase().replace(/[\s_-]/g, '');

export function isSensitiveKey(key: string): boolean {
  const normalized = normalizeKey(key);
  return SENSITIVE_KEYS.has(normalized) || SENSITIVE_SUFFIX.test(normalized);
}

const REDACTIONS: Array<[RegExp, string]> = [
  // SSE frames: the whole payload after `data:` is user/tool content, both
  // at the start of a line and embedded in an error message.
  [/\bdata:[^\n]*/g, 'data: [Filtered]'],
  // JSON Web Tokens (three base64url segments, header starts with eyJ).
  [/eyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}/g, '[jwt]'],
  // Authorization header values.
  [/\b(Bearer|Basic|Token)\s+[A-Za-z0-9._~+/=-]{8,}/gi, '$1 [Filtered]'],
  // Prefixed API keys (sk-..., pk_live_..., sbp_..., ghp_...).
  [/\b(sk|pk|rk|sbp|ghp|gho|xox[abp])[-_][A-Za-z0-9_-]{12,}/g, '[key]'],
  // Email addresses.
  [/[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/g, '[email]'],
  // Query strings and fragments on URLs.
  [/(https?:\/\/[^\s?#"'<>]+)[?#][^\s"'<>]*/g, '$1?[Filtered]'],
];

export function scrubString(value: string, maxLength = MAX_STRING_LENGTH): string {
  let out = value;
  for (const [pattern, replacement] of REDACTIONS) {
    out = out.replace(pattern, replacement);
  }
  if (out.length > maxLength) {
    out = `${out.slice(0, maxLength)}…[truncated ${out.length - maxLength} chars]`;
  }
  return out;
}

/** Remove the query string and fragment from a URL or path. */
export function stripQuery(url: string): string {
  const cut = url.search(/[?#]/);
  return cut === -1 ? url : url.slice(0, cut);
}

const isErrorLike = (
  value: unknown
): value is { name?: unknown; message?: unknown } =>
  value instanceof Error ||
  (typeof value === 'object' &&
    value !== null &&
    typeof (value as { stack?: unknown }).stack === 'string' &&
    typeof (value as { message?: unknown }).message === 'string');

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

/**
 * Structured data that is kept (event extra, breadcrumb data, non-console
 * log attributes): sensitive keys filtered, strings scrubbed, depth capped.
 */
export function scrubValue(value: unknown, depth = 0): unknown {
  if (value === null || value === undefined) return value;
  if (typeof value === 'string') return scrubString(value);
  if (typeof value !== 'object') return value;
  if (isErrorLike(value)) return scrubArgument(value);
  if (depth >= MAX_DEPTH) return OBJECT_PLACEHOLDER;
  if (Array.isArray(value)) {
    const items = value
      .slice(0, MAX_ARRAY_ITEMS)
      .map((item) => scrubValue(item, depth + 1));
    if (value.length > MAX_ARRAY_ITEMS) {
      items.push(`[${value.length - MAX_ARRAY_ITEMS} more]`);
    }
    return items;
  }
  const out: Record<string, unknown> = {};
  for (const [key, item] of Object.entries(value as Record<string, unknown>)) {
    out[key] = isSensitiveKey(key) ? FILTERED : scrubValue(item, depth + 1);
  }
  return out;
}

const looksSerialized = (message: string): boolean => /^\s*[{[]/.test(message);

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

export function scrubLog(log: Log): Log {
  const attributes = log.attributes ?? {};
  const scrubbedAttributes: Record<string, unknown> = {};
  const parameters: string[] = [];

  for (const [key, value] of Object.entries(attributes)) {
    if (key.startsWith(PARAMETER_PREFIX)) {
      const scrubbed = scrubArgument(value);
      scrubbedAttributes[key] = scrubbed;
      parameters[Number(key.slice(PARAMETER_PREFIX.length))] = asText(scrubbed);
    } else if (key === TEMPLATE_KEY) {
      scrubbedAttributes[key] =
        typeof value === 'string' ? scrubString(value) : FILTERED;
    } else if (key.startsWith('sentry.')) {
      // SDK-owned metadata (origin, sdk name/version, environment).
      scrubbedAttributes[key] = value;
    } else if (isSensitiveKey(key)) {
      scrubbedAttributes[key] = FILTERED;
    } else {
      // Keys spread from a plain-object first console argument.
      scrubbedAttributes[key] = scrubArgument(value);
    }
  }

  let message: string;
  const template = attributes[TEMPLATE_KEY];
  if (typeof template === 'string') {
    // Rebuild "first {} {}" from the scrubbed parameters, so an object
    // argument never reaches the formatted message.
    let index = 0;
    message = scrubString(
      template.replace(/\{\}/g, () => parameters[index++] ?? FILTERED)
    );
  } else {
    message = scrubConsoleMessage(asText(log.message));
  }

  return { ...log, message, attributes: scrubbedAttributes };
}

// ---------------------------------------------------------------------------
// Breadcrumbs
// ---------------------------------------------------------------------------

const URL_KEYS = ['url', 'from', 'to'];

export function scrubBreadcrumb(
  breadcrumb: Breadcrumb,
  _hint?: BreadcrumbHint
): Breadcrumb {
  const out: Breadcrumb = { ...breadcrumb };
  const isConsole = breadcrumb.category === 'console';

  if (typeof out.message === 'string') {
    out.message = isConsole
      ? scrubConsoleMessage(out.message)
      : scrubString(out.message);
  }

  if (out.data) {
    const data: Record<string, unknown> = {};
    for (const [key, value] of Object.entries(out.data)) {
      if (isConsole && key === 'arguments' && Array.isArray(value)) {
        data[key] = value.map(scrubArgument);
      } else if (URL_KEYS.includes(key) && typeof value === 'string') {
        // fetch/xhr/navigation URLs: never ship query strings.
        data[key] = scrubString(stripQuery(value));
      } else if (isSensitiveKey(key)) {
        data[key] = FILTERED;
      } else {
        data[key] = scrubValue(value, 1);
      }
    }
    out.data = data;
  }

  return out;
}

// ---------------------------------------------------------------------------
// Error events
// ---------------------------------------------------------------------------

const SAFE_HEADERS = new Set([
  'accept',
  'accept-language',
  'content-type',
  'content-length',
  'host',
  'referer',
  'user-agent',
]);

export function scrubEvent(event: ErrorEvent, _hint?: EventHint): ErrorEvent {
  const out: ErrorEvent = { ...event };

  if (typeof out.message === 'string') {
    out.message = scrubString(out.message, MAX_ERROR_MESSAGE_LENGTH);
  }

  if (out.exception?.values) {
    out.exception = {
      ...out.exception,
      values: out.exception.values.map((exception) => ({
        ...exception,
        value:
          typeof exception.value === 'string'
            ? scrubString(exception.value, MAX_ERROR_MESSAGE_LENGTH)
            : exception.value,
        stacktrace: exception.stacktrace && {
          ...exception.stacktrace,
          // Local variable values can hold any user content.
          frames: exception.stacktrace.frames?.map(({ vars: _vars, ...frame }) => frame),
        },
      })),
    };
  }

  if (out.request) {
    const { cookies: _cookies, data: _data, query_string: _query, ...request } =
      out.request;
    const headers: Record<string, string> = {};
    for (const [name, value] of Object.entries(request.headers ?? {})) {
      if (SAFE_HEADERS.has(name.toLowerCase())) {
        headers[name] =
          name.toLowerCase() === 'referer' ? stripQuery(value) : value;
      }
    }
    out.request = {
      ...request,
      url: request.url ? stripQuery(request.url) : request.url,
      headers,
    };
  }

  if (out.user) {
    out.user = out.user.id !== undefined ? { id: out.user.id } : {};
  }

  if (out.extra) {
    out.extra = scrubValue(out.extra) as ErrorEvent['extra'];
  }

  if (out.tags) {
    const tags: NonNullable<ErrorEvent['tags']> = {};
    for (const [key, value] of Object.entries(out.tags)) {
      tags[key] = typeof value === 'string' ? scrubString(value) : value;
    }
    out.tags = tags;
  }

  if (out.breadcrumbs) {
    out.breadcrumbs = out.breadcrumbs.map((crumb) => scrubBreadcrumb(crumb));
  }

  return out;
}

/** Spread into every Sentry.init so the hooks cannot drift apart. */
export const sentryScrubOptions = {
  beforeSend: scrubEvent,
  beforeBreadcrumb: scrubBreadcrumb,
  beforeSendLog: scrubLog,
};

/**
 * Console levels forwarded to Sentry Logs. Error only: warn and log are
 * mostly debug output from the chat and upload paths, which is where user
 * content has leaked before; errors are the level worth keeping.
 */
export const SENTRY_CONSOLE_LOG_LEVELS: Array<'error'> = ['error'];
