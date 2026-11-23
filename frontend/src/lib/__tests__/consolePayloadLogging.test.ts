/**
 * GOO-638 (FL-4): Sentry's consoleLoggingIntegration forwards console.log,
 * console.warn and console.error whenever a DSN is set, so a console call
 * that passes user content sends it to Sentry. No production code under
 * src/ or app/ may pass tool args/results, approval payloads, raw SSE frames,
 * message text, titles built from the first message or search queries to
 * the console.
 */
import { describe, expect, it } from 'vitest';
import { resolve } from 'node:path';

import {
  consoleCalls,
  payloadReferences,
  productionFiles,
  scanFile,
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
  ])('allows %s', (call) => {
    const [{ args }] = consoleCalls(call);
    expect(payloadReferences(args)).toEqual([]);
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
