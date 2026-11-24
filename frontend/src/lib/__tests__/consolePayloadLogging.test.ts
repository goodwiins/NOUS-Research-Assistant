/**
 * GOO-638 (FL-4): Sentry's consoleLoggingIntegration forwards console output
 * whenever a DSN is set, so a console call that passes user content sends it
 * to Sentry. No production code under src/ or app/ may pass tool
 * args/results, approval payloads, raw SSE frames, message text, titles
 * built from the first message, search queries or file names to the console.
 *
 * Scope: this is a regression tripwire, not a guarantee. It reads console
 * calls (`console.x(...)`, `console['x'](...)`, `console[method](...)`,
 * optional `console?.x(...)` / `console.x?.(...)`)
 * textually and does not follow aliasing, destructuring or values handed to
 * logging helpers. The guarantee is the Sentry-side scrubbing in GOO-725.
 *
 * Reviewed `console[level](...)` sinks (generic loggers; the scan passes them
 * because they forward a message and a data bag, so their callers matter):
 *  - security/frontendSecurity.ts SecurityLogger: logged `data`, which held
 *    the triggering input; now logs only id, severity and timestamp. The
 *    module is not imported anywhere today.
 *  - services/loggingService.ts logToConsole: forwards entry.message and
 *    entry.data. logSearchQuery no longer puts the query in either, and
 *    useSearchPerformance no longer interpolates it. Other callers
 *    (usePerformanceMonitoring, performanceMonitoring) log timings,
 *    component and metric names; metric contexts can still carry a query or
 *    file name, so performanceMonitoring's flush logs counts only.
 *  - utils/performanceMonitoring.ts flush: logged the whole sessionData
 *    (metrics with {query}/fileName contexts); now logs counts only.
 *  - utils/errorTracking.ts logToConsole: forwards message, Error and
 *    context; its callers (performanceMonitoring) pass metric names and
 *    flush errors.
 */
import { describe, expect, it } from 'vitest';
import { resolve } from 'node:path';

import {
  consoleCalls,
  payloadReferences,
  productionFiles,
  scanFile,
  stripStringText,
} from './consolePayloadScan';

const FRONTEND = resolve(__dirname, '../../..');

/** Reviewed exceptions. Each entry must say why the call cannot leak. */
const ALLOWED = new Set([
  // `debugLog` forwards its own arguments, but it is a no-op outside
  // NODE_ENV=development, so nothing reaches a deployed Sentry project.
  'src/hooks/useChatPersistence.ts:29 args',
]);

describe('console payload scanner', () => {
  it.each([
    ["console.log('[Agent] Tool start:', tool, args);", ['args']],
    [
      "console.log('[Agent] Tool end:', tool, result, { isError });",
      ['result'],
    ],
    ["console.warn('Malformed SSE line', dataLine, err);", ['dataLine']],
    ["console.log('[Chat] Switched to thread:', targetConv.title);", ['title']],
    ['console.log(`title is ${thread.title}`);', ['title']],
    ["console.log('needed:', confirmation);", ['confirmation']],
    [
      "console.error('[Chat] Upload failed for', file.name, err);",
      ['file.name'],
    ],
    ["console.error('Upload failed', item.file.name);", ['file.name']],
    ["console.warn('bad upload', upload.fileName);", ['fileName']],
    ["console.warn('bad upload', filename);", ['filename']],
    ["console['log']('[Agent] args', args);", ['args']],
    ['console["warn"](payload);', ['payload']],
    ['console[level](`[x] ${query}`);', ['query']],
    ['console.table(result);', ['result']],
    ['console.trace(content);', ['content']],
    ['console.group(title);', ['title']],
    ['console.dir(payload);', ['payload']],
    ['console.assert(ok, query);', ['query']],
    ['console?.log(args);', ['args']],
    ['console.warn?.(result);', ['result']],
    ["console?.['error'](body);", ['body']],
    ["console.log('flushed', sessionData);", ['sessionData']],
  ])('flags %s', (call, names) => {
    const [{ args }] = consoleCalls(call);
    expect(payloadReferences(args)).toEqual(names);
  });

  it.each([
    "console.log('[Chat] Switched to thread:', targetConv.id);",
    "console.warn('Skipped frame', { event: ev, length: dataLine.length });",
    "console.log('the args and result are hidden');",
    "console.error('Failed to load', error);",
    "console.warn('frame', { title: 'redacted' });",
    "console.warn('guard', { empty: !content });",
    "console.log('upload', { uploadId: item.id, size: file.size });",
    'console.log(`plain template, no file.name expression`);',
  ])('allows %s', (call) => {
    const [{ args }] = consoleCalls(call);
    expect(payloadReferences(args)).toEqual([]);
  });

  // GOO-728: a quote the scanner misread, or a bracket it could not match,
  // used to hide every later call in the file.
  it.each([
    [
      'an apostrophe in a block comment',
      "console.log('a', /* don't */ x);\nconsole.log(payload);",
    ],
    [
      'an apostrophe in a line comment',
      "console.log('a', x); // don't\nconsole.log(payload);",
    ],
    [
      'an apostrophe in a comment inside the call',
      "console.log('a', // don't\n  x);\nconsole.log(payload);",
    ],
    [
      'a regex containing a quote',
      "console.log(/'/.test(s));\nconsole.log(payload);",
    ],
    [
      'a regex with a quote in a class',
      'console.log(s.replace(/["]/g, ""));\nconsole.log(payload);',
    ],
    ['an unbalanced call', 'console.log(`oops);\nconsole.log(payload);'],
    [
      'an unclosed comment in a call',
      'console.log(a /* oops);\nconsole.log(payload);',
    ],
    [
      'a long unbalanced call',
      `console.log(f(${'x'.repeat(5_000)});\nconsole.log(payload);`,
    ],
    [
      'a long balanced call',
      `console.log(${'x'.repeat(5_000)});\nconsole.log(payload);`,
    ],
  ])('finds a later call after %s', (_shape, source) => {
    const calls = consoleCalls(source);
    const flagged = calls.filter(({ args }) =>
      payloadReferences(args).includes('payload')
    );
    // The payload call is always on the last line, and only it is flagged.
    expect(flagged.map((c) => c.line)).toEqual([source.split('\n').length]);
  });

  it.each([
    ["console.log('a', /* don't */ payload);", ['payload']],
    ["console.log(a /* don't */, payload);", ['payload']],
    ["console.log(x, // don't\n  payload);", ['payload']],
    ["console.log(/'/.test(s), payload);", ['payload']],
    ['console.log(s.split(/"/), payload);', ['payload']],
    ['console.log(n / 2, payload);', ['payload']],
    ["console.log('unterminated\n, payload);", ['payload']],
    [
      "console[isProductionBuild ? 'warn' : 'log' + loggingLevelSuffixForThisCall](payload);",
      ['payload'],
    ],
    ['console[\n  level\n](payload);', ['payload']],
    ['console[levels[kind]](payload);', ['payload']],
    ["console[']'.length ? 'log' : 'warn'](payload);", ['payload']],
  ])('flags %s (GOO-728)', (call, names) => {
    const [{ args }] = consoleCalls(call);
    expect(payloadReferences(args)).toEqual(names);
  });

  it.each([
    "console.log('x', /* the args */ id);",
    // After an identifier a `/` is a division, so only comment handling
    // keeps these words out.
    'console.log(id /* not the payload */);',
    'console.log(id // not the payload\n);',
    "console.log('x', id); // log the payload",
    'console.log(/title|query/.test(s));',
  ])('ignores comments and regex text: %s', (call) => {
    const [{ args }] = consoleCalls(call);
    expect(payloadReferences(args)).toEqual([]);
  });

  it('keeps template expressions and drops template text', () => {
    expect(stripStringText('`a ${x.title} b ${f({ y: 1 })}`, z')).toBe(
      '(x.title),(f({ y: 1 })), z'
    );
    expect(stripStringText(`'it\\'s args', "say \\"query\\""`)).toBe(
      '\'\', ""'
    );
  });

  // Each shape below was quadratic in an earlier version (up to 40s at this
  // size). The vitest timeout makes a regression fail instead of hanging CI.
  it.each([
    ['unclosed calls', 'console.log('.repeat(20_000)],
    ['unclosed brackets', 'console['.repeat(20_000)],
    ['many calls on many lines', 'console.log(a);\n'.repeat(20_000)],
    ['unclosed templates', '`${'.repeat(20_000)],
    ['escapes', "'" + '\\a'.repeat(50_000)],
    [
      'backslash and brace runs',
      '`' + '\\'.repeat(50_000) + '{'.repeat(20_000),
    ],
    ['name then whitespace', ('title' + ' '.repeat(50)).repeat(20_000)],
    // GOO-728: each unbalanced call is rescanned from its own arguments.
    ['unclosed comments in calls', 'console.log(/*'.repeat(20_000)],
    ['unclosed templates in calls', 'console.log(`'.repeat(20_000)],
    ['unclosed regex classes', 'console.log(/['.repeat(20_000)],
    ['regex starts on one line', '(/['.repeat(50_000)],
    ['unclosed quoted brackets', "console['".repeat(20_000)],
    ['long bracket bodies', ('console[' + 'x'.repeat(200)).repeat(2_000)],
    ['line comments in calls', 'console.log(//\n'.repeat(20_000)],
  ])(
    'stays linear on adversarial input: %s (CodeQL js/redos)',
    (_shape, hostile) => {
      const started = performance.now();
      for (const { args } of consoleCalls(hostile)) payloadReferences(args);
      payloadReferences(hostile);
      stripStringText(hostile);
      expect(performance.now() - started).toBeLessThan(1_000);
    },
    5_000
  );

  it('reports the right line for each call', () => {
    expect(
      consoleCalls('a;\nconsole.log(x);\n\nconsole.warn(y);').map((c) => c.line)
    ).toEqual([2, 4]);
  });
});

describe('production console calls', () => {
  it('scans a real tree', () => {
    expect(productionFiles(resolve(FRONTEND, 'src')).length).toBeGreaterThan(
      100
    );
  });

  it('never pass user payloads to the console', () => {
    const leaks = ['src', 'app']
      .flatMap((dir) => productionFiles(resolve(FRONTEND, dir)))
      .flatMap((file) => scanFile(file, FRONTEND))
      .map((leak) => `${leak.file}:${leak.line} ${leak.names.join(',')}`)
      .filter((leak) => !ALLOWED.has(leak));
    expect(leaks).toEqual([]);
  });
});
