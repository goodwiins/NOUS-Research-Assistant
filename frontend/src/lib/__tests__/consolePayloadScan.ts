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
 * the call. When it still cannot close a call (or a `console[...]` bracket)
 * within MAX_CALL_LENGTH characters, it gives up on that call and resumes
 * right after its opening parenthesis, so one unbalanced call never hides
 * the later calls in the file (GOO-728).
 *
 * Every pass is linear in the file size (CodeQL js/redos; the guard test runs
 * adversarial shapes under a time limit):
 *  - the call regex has no nested or adjacent overlapping quantifiers and no
 *    open-ended body; bracket and call bodies are matched by hand;
 *  - a body is followed for at most MAX_CALL_LENGTH characters and a regex
 *    literal for at most MAX_REGEX_LENGTH, and each literal's end is measured
 *    once per file, however many unbalanced calls rescan it;
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
 * bracket body is matched by hand (closingIndex), not by the regex, so it can
 * be any length and span lines without letting a run of unclosed `console[`
 * make each match scan to the end of the file.
 */
const CONSOLE_CALL =
  /console\s*(?:\??\.\s*(?:log|info|debug|warn|error|table|trace|group|groupCollapsed|dir|dirxml|assert)\b\s*(?:\?\.\s*)?\(|(?:\?\.\s*)?\[)/g;

/**
 * How far one call (or bracket) body is followed before the call counts as
 * unbalanced. The longest real console call in the tree is about 650
 * characters; the bound is what keeps a run of unclosed calls linear.
 */
const MAX_CALL_LENGTH = 2_000;

/** How far a `/` is followed to find the end of a regex literal. */
const MAX_REGEX_LENGTH = 256;

const NEWLINE = 10;
const DOUBLE_QUOTE = 34;
const SINGLE_QUOTE = 39;
const STAR = 42;
const SLASH = 47;
const BACKSLASH = 92;
const BACKTICK = 96;
const OPEN_BRACKET = 91;
const CLOSE_BRACKET = 93;
const OPEN_PAREN = 40;
const CLOSE_PAREN = 41;

/** After one of these (or at the start), `/` opens a regex, not a division. */
const REGEX_PRECEDERS = new Uint8Array(128);
for (const ch of '(,=:[!&|?{};+-*%<>~^') REGEX_PRECEDERS[ch.charCodeAt(0)] = 1;

function regexMayFollow(prev: number): boolean {
  return prev === 0 || REGEX_PRECEDERS[prev] === 1;
}

function opensComment(source: string, i: number): boolean {
  const next = source.charCodeAt(i + 1);
  return source.charCodeAt(i) === SLASH && (next === SLASH || next === STAR);
}

/**
 * Where the comment, string, template or regex literal opening at `i` ends
 * (exclusive), or `i` when none opens there. `prev` is the char code of the
 * last significant character before `i` (0 for none). A `'` or `"` string
 * ends at an unescaped newline (JavaScript strings cannot span lines), so a
 * misread quote cannot run on. Every scan stops at `limit`.
 */
function literalEnd(
  source: string,
  i: number,
  prev: number,
  limit: number
): number {
  const ch = source.charCodeAt(i);
  let j = i + 1;
  if (ch === SINGLE_QUOTE || ch === DOUBLE_QUOTE || ch === BACKTICK) {
    while (j < limit) {
      const c = source.charCodeAt(j);
      if (c === ch) return j + 1;
      if (c === NEWLINE && ch !== BACKTICK) return j;
      j += c === BACKSLASH ? 2 : 1;
    }
    return limit;
  }
  if (ch !== SLASH) return i;
  const next = source.charCodeAt(j);
  if (next === SLASH) {
    while (j < limit && source.charCodeAt(j) !== NEWLINE) j += 1;
    return j;
  }
  if (next === STAR) {
    j += 1;
    while (j < limit) {
      if (source.charCodeAt(j) === STAR && source.charCodeAt(j + 1) === SLASH) {
        return Math.min(j + 2, limit);
      }
      j += 1;
    }
    return limit;
  }
  if (!regexMayFollow(prev)) return i;
  const stop = Math.min(limit, i + MAX_REGEX_LENGTH);
  let inClass = false;
  while (j < stop) {
    const c = source.charCodeAt(j);
    if (c === NEWLINE) break;
    if (c === BACKSLASH) j += 1;
    else if (inClass) inClass = c !== CLOSE_BRACKET;
    else if (c === OPEN_BRACKET) inClass = true;
    else if (c === SLASH) return j + 1;
    j += 1;
  }
  return i; // no closing slash on this line: a division after all
}

/**
 * `literalEnd` for the call scanner, memoised per file in `ends` (end + 1,
 * 0 for not yet measured). Each entry is measured up to MAX_CALL_LENGTH past
 * its own start, which reaches every window that contains it, so unbalanced
 * calls that rescan the same text measure each literal only once.
 */
function cachedLiteralEnd(
  source: string,
  i: number,
  prev: number,
  ends: Int32Array
): number {
  if (
    source.charCodeAt(i) === SLASH &&
    !opensComment(source, i) &&
    !regexMayFollow(prev)
  ) {
    return i; // a division; the cached regex answer assumed otherwise
  }
  const cached = ends[i];
  if (cached) return cached - 1;
  const limit = Math.min(source.length, i + MAX_CALL_LENGTH);
  const end = literalEnd(source, i, prev, limit);
  ends[i] = end + 1;
  return end;
}

/**
 * Index of the `close` that balances an `open` just before `start`, skipping
 * literals and comments, or -1 when there is none within MAX_CALL_LENGTH.
 */
function closingIndex(
  source: string,
  start: number,
  open: number,
  close: number,
  ends: Int32Array
): number {
  const limit = Math.min(source.length, start + MAX_CALL_LENGTH);
  let depth = 1;
  let prev = open;
  let i = start;
  while (i < limit) {
    const ch = source.charCodeAt(i);
    if (
      ch === SLASH ||
      ch === SINGLE_QUOTE ||
      ch === DOUBLE_QUOTE ||
      ch === BACKTICK
    ) {
      const end = cachedLiteralEnd(source, i, prev, ends);
      if (end > i) {
        if (!opensComment(source, i)) prev = source.charCodeAt(end - 1);
        i = end;
        continue;
      }
    } else if (ch === open) {
      depth += 1;
    } else if (ch === close && --depth === 0) {
      return i;
    }
    if (ch > 32) prev = ch;
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
  ends: Int32Array
): { index: number; argsStart: number } | null {
  CONSOLE_CALL.lastIndex = from;
  let match: RegExpExecArray | null;
  while ((match = CONSOLE_CALL.exec(source))) {
    const afterMatch = CONSOLE_CALL.lastIndex;
    if (!match[0].endsWith('[')) {
      return { index: match.index, argsStart: afterMatch };
    }
    const bracketEnd = closingIndex(
      source,
      afterMatch,
      OPEN_BRACKET,
      CLOSE_BRACKET,
      ends
    );
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
  const ends = new Int32Array(source.length + 1);
  let line = 1;
  let counted = 0;
  let call = nextCall(source, 0, ends);
  while (call) {
    for (; counted < call.index; counted += 1) {
      if (source.charCodeAt(counted) === NEWLINE) line += 1;
    }
    const { argsStart } = call;
    const end = closingIndex(source, argsStart, OPEN_PAREN, CLOSE_PAREN, ends);
    if (end !== -1) {
      calls.push({ line, args: source.slice(argsStart, end) });
      // Resume after this call: nested console calls are inside its args.
      call = nextCall(source, end + 1, ends);
      continue;
    }
    // Unbalanced: keep its text up to the next call, then go on from its own
    // arguments so it cannot hide the calls after it.
    call = nextCall(source, argsStart, ends);
    const stop = Math.min(
      call ? call.index : source.length,
      argsStart + MAX_CALL_LENGTH
    );
    calls.push({ line, args: source.slice(argsStart, stop) });
  }
  return calls;
}

/**
 * Drop string-literal text, comments and regex literals but keep `${...}`
 * expressions from templates, so words inside messages ("the args are
 * hidden") are not mistaken for values. A hand-written single pass: each
 * character is visited once, except that a `/` that turns out to be a
 * division was first followed for at most MAX_REGEX_LENGTH characters.
 */
export function stripStringText(args: string): string {
  let out = '';
  let prev = 0;
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
      prev = BACKTICK;
      continue;
    }
    const end = literalEnd(args, i, prev, args.length);
    if (end > i) {
      if (opensComment(args, i)) {
        out += ' ';
      } else {
        out += ch === "'" || ch === '"' ? `${ch}${ch}` : '/ /';
        prev = args.charCodeAt(end - 1);
      }
      i = end;
      continue;
    }
    out += ch;
    if (args.charCodeAt(i) > 32) prev = args.charCodeAt(i);
    i += 1;
  }
  return out;
}

/** Payload names a console call's arguments reference, if any. */
export function payloadReferences(args: string): string[] {
  const code = stripStringText(args);
  const names: string[] = PAYLOAD_NAMES.filter((name) =>
    // A bare value or a property read; not an object key (`title: ...`), a
    // boolean test (`!content`) or just its size (`dataLine.length`).
    new RegExp(
      `(?:^|[^\\w$.!]|\\.)${name}\\b(?!\\s*:)(?!\\??\\.length\\b)`
    ).test(code)
  );
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
