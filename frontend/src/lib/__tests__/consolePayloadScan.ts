/**
 * Source scanner behind consolePayloadLogging.test.ts (GOO-638).
 *
 * Sentry's consoleLoggingIntegration ships console.log/warn/error to Sentry
 * whenever a DSN is set, so a console call that passes user content (tool
 * args/results, approval payloads, raw SSE frames, message text, titles
 * built from the first message, search queries) leaks it off the device.
 * This finds console calls whose arguments reference such values, either as
 * a bare identifier or as a property (`thread.title`).
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
] as const;

const CONSOLE_CALL = /console\.(?:log|info|debug|warn|error)\s*\(/g;

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

/** Drop string literal text but keep `${...}` expressions in templates. */
function stripStringText(args: string): string {
  return args
    .replace(/'(?:\\.|[^'\\])*'/g, "''")
    .replace(/"(?:\\.|[^"\\])*"/g, '""')
    .replace(/`(?:\\.|\$\{([^}]*)\}|[^`\\])*`/g, (tpl) =>
      Array.from(tpl.matchAll(/\$\{([^}]*)\}/g), (m) => `(${m[1]})`).join(',')
    );
}

/** Payload names a console call's arguments reference, if any. */
export function payloadReferences(args: string): string[] {
  const code = stripStringText(args);
  return PAYLOAD_NAMES.filter((name) =>
    // A bare value or a property read; not an object key (`title: ...`), a
    // boolean test (`!content`) or just its size (`dataLine.length`).
    new RegExp(
      `(?:^|[^\\w$.!]|\\.)${name}\\b(?!\\s*:)(?!\\??\\.length\\b)`
    ).test(code)
  );
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
    return /\.(ts|tsx)$/.test(name) &&
      !/\.(test|spec|stories)\.(ts|tsx)$/.test(name)
      ? [path]
      : [];
  });
}
