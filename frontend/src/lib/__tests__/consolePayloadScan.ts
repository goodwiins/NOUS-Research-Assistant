/**
 * Source scanner behind consolePayloadLogging.test.ts (GOO-638).
 *
 * Sentry's consoleLoggingIntegration ships console output to Sentry whenever
 * a DSN is set, so a console call that passes user content (tool
 * args/results, approval payloads, raw SSE frames, message text, titles built
 * from the first message, search queries, file names) leaks it off the
 * device. This finds console calls whose arguments reference such values,
 * either as a bare identifier or as a property (`thread.title`).
 *
 * It is a regression tripwire, not a guarantee: it does not follow aliasing,
 * destructuring or values passed through helpers. The guarantee is the
 * Sentry-side scrubbing tracked in GOO-725.
 *
 * The tokenizer skips comments, strings, templates and regex literals, so an
 * apostrophe in a comment or a quote in a regex does not swallow the rest of
 * a call, and a `'` or `"` string ends at a newline, so a misread quote costs
 * one line at most. A call that never closes keeps its text up to the next
 * call, and the scan goes on from its own arguments, so one unbalanced call
 * cannot hide the calls after it (GOO-728).
 *
 * Every pass is linear in the file size (CodeQL js/redos; the guard test runs
 * adversarial shapes under a time limit, and CI runs it under coverage, which
 * slows tight loops a lot, so "linear" has to mean no rescanning at all):
 *  - the call regex has no nested or adjacent overlapping quantifiers and no
 *    open-ended body;
 *  - one tokenizer pass per file pairs every `(` and `[` outside literals
 *    with its close, so a call's end is a lookup, not a rescan;
 *  - a call that opens inside a comment or string (commented-out code) is
 *    scanned on its own, only up to the next `console`, so those scans never
 *    overlap;
 *  - a `/` that turns out not to open a regex literal stops regex detection
 *    until the end of that line, so its line is not scanned again;
 *  - the args kept for an unbalanced call stop at the next call;
 *  - line numbers are counted incrementally.
 */
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join, relative } from 'node:path';

export const PAYLOAD_NAMES = [
  'args',
  'toolArgs',
  'tool_args',
  'result',
  'toolResult',
  'confirmation',
  'payload',
  'dataLine',
  'rawLine',
  'content',
  'title',
  'dynamicTitle',
  'body',
  'prompt',
  'query',
  'fileName',
  'filename',
  'sessionData',
] as const;

/** `file.name` / `files.name` / `file?.name`, including `item.file.name`. */
const FILE_NAME_ACCESS = /\bfiles?\??\.name\b/;

/**
 * The start of `console.log(`, `console.table(`, `console?.log(`,
 * `console.log?.(`, or of a bracket access `console[` / `console?.[`. The
 * bracket body is matched by the tokenizer, not by the regex, so it can be
 * any length and span lines without letting a run of unclosed `console[`
 * make each match scan to the end of the file.
 */
const CONSOLE_CALL =
  /console\s*(?:\??\.\s*(?:log|info|debug|warn|error|table|trace|group|groupCollapsed|dir|dirxml|assert)\b\s*(?:\?\.\s*)?\(|(?:\?\.\s*)?\[)/g;

const NEWLINE = 10;
const DOUBLE_QUOTE = 34;
const SINGLE_QUOTE = 39;
const OPEN_PAREN = 40;
const CLOSE_PAREN = 41;
const STAR = 42;
const SLASH = 47;
const OPEN_BRACKET = 91;
const BACKSLASH = 92;
const CLOSE_BRACKET = 93;
const BACKTICK = 96;

/** After one of these (or at the start), `/` opens a regex, not a division. */
const REGEX_PRECEDERS = new Uint8Array(128);
for (const ch of '(,=:[!&|?{};+-*%<>~^') REGEX_PRECEDERS[ch.charCodeAt(0)] = 1;

/**
 * One left-to-right walk over a text. `prev` is the char code of the last
 * significant character outside literals (0 at the start); `noRegexUntil`
 * marks the rest of a line where a `/` already failed to open a regex.
 */
interface Walk {
  prev: number;
  noRegexUntil: number;
}

/** Characters that may open a comment, string, template or regex. */
const LITERAL_START = new Uint8Array(128);
for (const ch of `/'"\``) LITERAL_START[ch.charCodeAt(0)] = 1;

function opensComment(source: string, i: number): boolean {
  const next = source.charCodeAt(i + 1);
  return source.charCodeAt(i) === SLASH && (next === SLASH || next === STAR);
}

/**
 * Where the comment, string, template or regex literal opening at `i` ends
 * (exclusive), or `i` when none opens there; updates `walk.prev`. A `'` or
 * `"` string ends at an unescaped newline (JavaScript strings cannot span
 * lines). Templates are opaque, as before. Every scan stops at `limit`.
 */
function skipLiteral(
  source: string,
  i: number,
  limit: number,
  walk: Walk
): number {
  const ch = source.charCodeAt(i);
  let j = i + 1;
  if (ch === SINGLE_QUOTE || ch === DOUBLE_QUOTE || ch === BACKTICK) {
    for (; j < limit; j += source.charCodeAt(j) === BACKSLASH ? 2 : 1) {
      const c = source.charCodeAt(j);
      if (c === ch) {
        j += 1;
        break;
      }
      if (c === NEWLINE && ch !== BACKTICK) break;
    }
    walk.prev = ch;
    return Math.min(j, limit);
  }
  if (ch !== SLASH) return i;
  const next = source.charCodeAt(j);
  if (next === SLASH) {
    while (j < limit && source.charCodeAt(j) !== NEWLINE) j += 1;
    return j;
  }
  if (next === STAR) {
    for (j += 1; j < limit; j += 1) {
      if (source.charCodeAt(j) === STAR && source.charCodeAt(j + 1) === SLASH) {
        return Math.min(j + 2, limit);
      }
    }
    return limit;
  }
  if (
    i < walk.noRegexUntil ||
    (walk.prev !== 0 && REGEX_PRECEDERS[walk.prev] !== 1)
  ) {
    return i;
  }
  let inClass = false;
  for (; j < limit; j += 1) {
    const c = source.charCodeAt(j);
    if (c === NEWLINE) break;
    if (c === BACKSLASH) j += 1;
    else if (inClass) inClass = c !== CLOSE_BRACKET;
    else if (c === OPEN_BRACKET) inClass = true;
    else if (c === SLASH) {
      walk.prev = SLASH;
      return j + 1;
    }
  }
  // No closing slash on this line: a division, and so is any later `/` on
  // it that would have been read the same way.
  walk.noRegexUntil = j;
  return i;
}

/**
 * The file's bracket structure: `code[i]` is 1 when `i` is outside every
 * comment and literal, and `close[i]` is the index of the `)` or `]` that
 * closes the `(` or `[` at `i` (-1 when it never closes).
 */
interface Structure {
  code: Uint8Array;
  close: Int32Array;
}

function structure(source: string): Structure {
  const code = new Uint8Array(source.length);
  const close = new Int32Array(source.length).fill(-1);
  const parens: number[] = [];
  const brackets: number[] = [];
  const walk: Walk = { prev: 0, noRegexUntil: 0 };
  let i = 0;
  while (i < source.length) {
    const ch = source.charCodeAt(i);
    if (LITERAL_START[ch] === 1) {
      const end = skipLiteral(source, i, source.length, walk);
      if (end > i) {
        i = end;
        continue;
      }
    }
    code[i] = 1;
    if (ch === OPEN_PAREN) parens.push(i);
    else if (ch === OPEN_BRACKET) brackets.push(i);
    else if (ch === CLOSE_PAREN || ch === CLOSE_BRACKET) {
      const open = (ch === CLOSE_PAREN ? parens : brackets).pop();
      if (open !== undefined) close[open] = i;
    }
    if (ch > 32) walk.prev = ch;
    i += 1;
  }
  return { code, close };
}

/**
 * Index of the bracket that closes the `(` or `[` at `open`, or -1. Outside
 * literals it is a lookup. Inside one (a commented-out or quoted call) the
 * call is tokenized on its own, and only up to the next `console`, so these
 * scans never overlap.
 */
function closeOf(source: string, open: number, shape: Structure): number {
  if (shape.code[open]) return shape.close[open];
  const opener = source.charCodeAt(open);
  const closer = opener === OPEN_PAREN ? CLOSE_PAREN : CLOSE_BRACKET;
  const nextConsole = source.indexOf('console', open);
  const limit = nextConsole === -1 ? source.length : nextConsole;
  const walk: Walk = { prev: opener, noRegexUntil: 0 };
  let depth = 1;
  let i = open + 1;
  while (i < limit) {
    const ch = source.charCodeAt(i);
    if (LITERAL_START[ch] === 1) {
      const end = skipLiteral(source, i, limit, walk);
      if (end > i) {
        i = end;
        continue;
      }
    }
    if (ch === opener) depth += 1;
    else if (ch === closer && --depth === 0) return i;
    if (ch > 32) walk.prev = ch;
    i += 1;
  }
  return -1;
}

function skipSpaces(source: string, i: number): number {
  while (i < source.length && source.charCodeAt(i) <= 32) i += 1;
  return i;
}

/**
 * Just past the `(` (or `?.(`) that turns `console[...]` into a call when it
 * follows `i`, otherwise -1.
 */
function callOpenAfter(source: string, i: number): number {
  let j = skipSpaces(source, i);
  if (source[j] === '?' && source[j + 1] === '.') j = skipSpaces(source, j + 2);
  return source.charCodeAt(j) === OPEN_PAREN ? j + 1 : -1;
}

/** The next console call at or after `from`: where it starts and its args. */
function nextCall(
  source: string,
  from: number,
  shape: Structure
): { index: number; argsStart: number } | null {
  CONSOLE_CALL.lastIndex = from;
  let match: RegExpExecArray | null;
  while ((match = CONSOLE_CALL.exec(source))) {
    const afterMatch = CONSOLE_CALL.lastIndex;
    if (!match[0].endsWith('[')) {
      return { index: match.index, argsStart: afterMatch };
    }
    const bracketEnd = closeOf(source, afterMatch - 1, shape);
    const argsStart =
      bracketEnd === -1 ? -1 : callOpenAfter(source, bracketEnd + 1);
    if (argsStart !== -1) return { index: match.index, argsStart };
    // Not a call (`console[x] = y`) or no closing bracket: keep looking
    // inside the bracket, as a regex mismatch would.
    CONSOLE_CALL.lastIndex = afterMatch;
  }
  return null;
}

/** Every console call's argument text, with its 1-based line number. */
export function consoleCalls(
  source: string
): Array<{ line: number; args: string }> {
  const calls: Array<{ line: number; args: string }> = [];
  const shape = structure(source);
  let line = 1;
  let counted = 0;
  let call = nextCall(source, 0, shape);
  while (call) {
    for (; counted < call.index; counted += 1) {
      if (source.charCodeAt(counted) === NEWLINE) line += 1;
    }
    const { argsStart } = call;
    const end = closeOf(source, argsStart - 1, shape);
    if (end !== -1) {
      calls.push({ line, args: source.slice(argsStart, end) });
      // Resume after this call: nested console calls are inside its args.
      call = nextCall(source, end + 1, shape);
      continue;
    }
    // Unbalanced: keep its text up to the next call, then go on from its own
    // arguments so it cannot hide the calls after it.
    call = nextCall(source, argsStart, shape);
    calls.push({
      line,
      args: source.slice(argsStart, call ? call.index : source.length),
    });
  }
  return calls;
}

/**
 * Drop string-literal text, comments and regex literals but keep `${...}`
 * expressions from templates, so words inside messages ("the args are
 * hidden") are not mistaken for values. A hand-written single pass: each
 * character is visited once (a `/` that is not a regex stops regex
 * detection for the rest of its line).
 */
export function stripStringText(args: string): string {
  let out = '';
  const walk: Walk = { prev: 0, noRegexUntil: 0 };
  let i = 0;
  while (i < args.length) {
    const ch = args[i];
    if (ch === '`') {
      const expressions: string[] = [];
      i += 1;
      while (i < args.length && args[i] !== '`') {
        if (args[i] === '\\') {
          i += 2;
        } else if (args[i] === '$' && args[i + 1] === '{') {
          i += 2;
          const start = i;
          let depth = 1;
          while (i < args.length && depth > 0) {
            if (args[i] === '{') depth += 1;
            else if (args[i] === '}') depth -= 1;
            i += 1;
          }
          expressions.push(`(${args.slice(start, i - 1)})`);
        } else {
          i += 1;
        }
      }
      i += 1; // closing backtick
      out += expressions.length ? expressions.join(',') : "''";
      walk.prev = BACKTICK;
      continue;
    }
    const code = args.charCodeAt(i);
    const end =
      LITERAL_START[code] === 1 ? skipLiteral(args, i, args.length, walk) : i;
    if (end > i) {
      if (opensComment(args, i)) out += ' ';
      else out += ch === "'" || ch === '"' ? `${ch}${ch}` : '/ /';
      i = end;
      continue;
    }
    out += ch;
    if (code > 32) walk.prev = code;
    i += 1;
  }
  return out;
}

/**
 * A bare value or a property read of each payload name; not an object key
 * (`title: ...`), a boolean test (`!content`) or just its size
 * (`dataLine.length`). Compiled once: the scan runs them for every call.
 */
const PAYLOAD_PATTERNS = PAYLOAD_NAMES.map(
  (name) =>
    [
      name,
      new RegExp(`(?:^|[^\\w$.!]|\\.)${name}\\b(?!\\s*:)(?!\\??\\.length\\b)`),
    ] as const
);

/** Payload names a console call's arguments reference, if any. */
export function payloadReferences(args: string): string[] {
  const code = stripStringText(args);
  const names: string[] = PAYLOAD_PATTERNS.filter(([, pattern]) =>
    pattern.test(code)
  ).map(([name]) => name);
  if (FILE_NAME_ACCESS.test(code)) names.push('file.name');
  return names;
}

export interface Leak {
  file: string;
  line: number;
  names: string[];
}

export function scanFile(path: string, root: string): Leak[] {
  const source = readFileSync(path, 'utf8');
  return consoleCalls(source)
    .map(({ line, args }) => ({
      file: relative(root, path),
      line,
      names: payloadReferences(args),
    }))
    .filter((leak) => leak.names.length > 0);
}

export function productionFiles(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) {
      return name === '__tests__' || name === 'node_modules'
        ? []
        : productionFiles(path);
    }
    return /\.(ts|tsx|js|jsx)$/.test(name) &&
      !/\.(test|spec|stories)\.(ts|tsx|js|jsx)$/.test(name)
      ? [path]
      : [];
  });
}
