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
 * Every pass here is a single linear scan or a regex without nested or
 * overlapping quantifiers, so no input can make it backtrack (CodeQL
 * js/redos).
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
] as const;

/** `file.name` / `files.name` / `file?.name`, including `item.file.name`. */
const FILE_NAME_ACCESS = /\bfiles?\??\.name\b/;

/**
 * `console.log(`, `console.table(`, `console['warn'](`, `console[method](`.
 * `[^\]]*` cannot overlap with the surrounding literals, so this is linear.
 */
const CONSOLE_CALL =
  /console\s*(?:\.\s*(?:log|info|debug|warn|error|table|trace)|\[[^\]]*\])\s*\(/g;

/** Every console call's argument text, with its 1-based line number. */
export function consoleCalls(
  source: string
): Array<{ line: number; args: string }> {
  const calls: Array<{ line: number; args: string }> = [];
  CONSOLE_CALL.lastIndex = 0;
  let match: RegExpExecArray | null;
  while ((match = CONSOLE_CALL.exec(source))) {
    let depth = 1;
    let i = CONSOLE_CALL.lastIndex;
    let quote: string | null = null;
    while (i < source.length && depth > 0) {
      const ch = source[i];
      if (quote) {
        if (ch === '\\') i += 1;
        else if (ch === quote) quote = null;
      } else if (ch === "'" || ch === '"' || ch === '`') quote = ch;
      else if (ch === '(') depth += 1;
      else if (ch === ')') depth -= 1;
      i += 1;
    }
    calls.push({
      line: source.slice(0, match.index).split('\n').length,
      args: source.slice(CONSOLE_CALL.lastIndex, i - 1),
    });
  }
  return calls;
}

/**
 * Drop string-literal text but keep `${...}` expressions from templates, so
 * words inside messages ("the args are hidden") are not mistaken for values.
 * A hand-written single pass: each character is visited once.
 */
export function stripStringText(args: string): string {
  let out = '';
  let i = 0;
  while (i < args.length) {
    const ch = args[i];
    if (ch === "'" || ch === '"') {
      i += 1;
      while (i < args.length && args[i] !== ch) i += args[i] === '\\' ? 2 : 1;
      i += 1; // closing quote
      out += `${ch}${ch}`;
      continue;
    }
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
      continue;
    }
    out += ch;
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
