/**
 * GOO-638 (FL-4): Sentry's consoleLoggingIntegration forwards console output
 * whenever a DSN is set, so a console call that passes user content sends it
 * to Sentry. No production code under src/ or app/ may pass tool
 * args/results, approval payloads, raw SSE frames, message text, titles
 * built from the first message, search queries or file names to the console.
 *
 * Scope: this is a regression tripwire, not a guarantee. It reads console
 * calls (`console.x(...)`, `console['x'](...)`, `console[method](...)`)
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
 *    useSearchPerformance no longer interpolates it. Reached only from
 *    usePerformanceMonitoring / performanceMonitoring, which log timings,
 *    component names and metric names.
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
    ["console.log('[Agent] Tool end:', tool, result, { isError });", ['result']],
    ["console.warn('Malformed SSE line', dataLine, err);", ['dataLine']],
    ["console.log('[Chat] Switched to thread:', targetConv.title);", ['title']],
    ['console.log(`title is ${thread.title}`);', ['title']],
    ["console.log('needed:', confirmation);", ['confirmation']],
    ["console.error('[Chat] Upload failed for', file.name, err);", ['file.name']],
    ["console.error('Upload failed', item.file.name);", ['file.name']],
    ["console.warn('bad upload', upload.fileName);", ['fileName']],
    ["console.warn('bad upload', filename);", ['filename']],
    ["console['log']('[Agent] args', args);", ['args']],
    ['console["warn"](payload);', ['payload']],
    ['console[level](`[x] ${query}`);', ['query']],
    ['console.table(result);', ['result']],
    ['console.trace(content);', ['content']],
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
    "console.log(`plain template, no file.name expression`);",
  ])('allows %s', (call) => {
    const [{ args }] = consoleCalls(call);
    expect(payloadReferences(args)).toEqual([]);
  });

  it('keeps template expressions and drops template text', () => {
    expect(stripStringText('`a ${x.title} b ${f({ y: 1 })}`, z')).toBe(
      '(x.title),(f({ y: 1 })), z'
    );
    expect(stripStringText(`'it\\'s args', "say \\"query\\""`)).toBe(
      "'', \"\""
    );
  });

  it('stays linear on adversarial input (CodeQL js/redos)', () => {
    const hostile = [
      '`' + '\\'.repeat(50_000) + '${'.repeat(20_000) + '{'.repeat(20_000),
      "'" + '\\a'.repeat(50_000),
      'console[' + '['.repeat(50_000),
      'console.log(' + '('.repeat(50_000),
      'x'.repeat(100_000) + 'title',
    ].join('\n');
    const started = performance.now();
    for (const { args } of consoleCalls(hostile)) payloadReferences(args);
    payloadReferences(hostile);
    expect(performance.now() - started).toBeLessThan(1_000);
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
