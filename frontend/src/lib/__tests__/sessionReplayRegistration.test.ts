/**
 * Session replay may only run with the approved privacy options (GOO-774).
 *
 * Sentry's replay records with `REPLAY_PRIVACY_OPTIONS`: text and media
 * masked, iframes and query-carrying links blocked, URLs scrubbed (GOO-725,
 * GOO-734, GOO-746). Any other recorder bypasses all of that. Statsig's
 * `StatsigSessionReplayPlugin()` once ran with no options at all, so chat
 * text, document content, search text and URL tokens could leave the browser
 * through it.
 *
 * This scans the app's shipped source for every way to register a session
 * recorder and allows exactly one: `replayIntegration(REPLAY_PRIVACY_OPTIONS)`
 * in `instrumentation-client.ts`. Re-enabling Statsig replay (or adding any
 * other recorder) needs a privacy config at least as strict as Sentry's,
 * verified in a real browser, and an entry here in the same PR.
 */
import { readdirSync, readFileSync, statSync } from 'node:fs';
import path from 'node:path';
import { describe, expect, it } from 'vitest';

const ROOT = path.resolve(__dirname, '../../..');
const SOURCE_DIRS = ['app', 'src'];
const SOURCE_FILE = /\.(ts|tsx|js|jsx|mjs|cjs)$/;
const SKIPPED_DIRS = new Set(['node_modules', '.next', '__tests__', '__mocks__']);
const TEST_FILE = /\.(test|spec)\.[jt]sx?$/;

/** Packages that record the DOM or the session. */
const RECORDER_PACKAGE =
  /^(@statsig\/session-replay|rrweb|rrweb-snapshot|@rrweb\/.+|posthog-js|logrocket|@fullstory\/.+|@hotjar\/.+|@openreplay\/.+|@microsoft\/clarity|smartlook-client|mouseflow.*|@sentry-internal\/replay.*)$/;

/** Ways source code can start a recorder. */
const RECORDER_PATTERNS: readonly RegExp[] = [
  /['"](@statsig\/session-replay|rrweb|rrweb-snapshot|@rrweb\/[^'"]+|posthog-js|logrocket|@fullstory\/[^'"]+|@hotjar\/[^'"]+|@openreplay\/[^'"]+|@microsoft\/clarity|smartlook-client|mouseflow[^'"]*|@sentry-internal\/replay[^'"]*)['"]/g,
  /\b\w*SessionReplay\w*\s*\(/g,
  /\breplay(Canvas)?Integration\s*\([^)]*\)/g,
];

/** The only recorder allowed to run, with the options it must run with. */
const APPROVED = [
  {
    file: 'instrumentation-client.ts',
    match: 'replayIntegration(REPLAY_PRIVACY_OPTIONS)',
  },
];

function walk(dir: string, out: string[]): string[] {
  for (const entry of readdirSync(dir)) {
    const full = path.join(dir, entry);
    if (statSync(full).isDirectory()) {
      if (!SKIPPED_DIRS.has(entry)) walk(full, out);
    } else if (SOURCE_FILE.test(entry) && !TEST_FILE.test(entry)) {
      out.push(full);
    }
  }
  return out;
}

function shippedSourceFiles(): string[] {
  const files: string[] = [];
  for (const dir of SOURCE_DIRS) walk(path.join(ROOT, dir), files);
  // Root-level entry points: instrumentation, Sentry and Next configs.
  for (const entry of readdirSync(ROOT)) {
    if (SOURCE_FILE.test(entry) && !TEST_FILE.test(entry)) {
      files.push(path.join(ROOT, entry));
    }
  }
  return files;
}

/** Drop comments, so prose that names a recorder is not a registration. */
function stripComments(source: string): string {
  return source
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/(^|[^:'"`])\/\/.*$/gm, '$1');
}

function recorderRegistrations(): Array<{ file: string; match: string }> {
  const found: Array<{ file: string; match: string }> = [];
  for (const file of shippedSourceFiles()) {
    const source = stripComments(readFileSync(file, 'utf8'));
    for (const pattern of RECORDER_PATTERNS) {
      for (const match of source.matchAll(pattern)) {
        found.push({
          file: path.relative(ROOT, file),
          match: match[0].replace(/\s+/g, ''),
        });
      }
    }
  }
  return found;
}

const isApproved = (hit: { file: string; match: string }): boolean =>
  APPROVED.some(
    (approved) =>
      approved.file === hit.file && hit.match.endsWith(approved.match)
  );

describe('session replay registration (GOO-774)', () => {
  it('starts no session recorder other than the approved Sentry replay', () => {
    const unapproved = recorderRegistrations().filter((hit) => !isApproved(hit));

    expect(unapproved).toEqual([]);
  });

  it('still finds the approved Sentry replay, so the scan is live', () => {
    const hits = recorderRegistrations();

    expect(hits.filter(isApproved)).toHaveLength(APPROVED.length);
  });

  it('flags a recorder registered without the approved options', () => {
    const source = stripComments(
      [
        "import { StatsigSessionReplayPlugin } from '@statsig/session-replay';",
        'const plugins = [new StatsigSessionReplayPlugin()];',
        'Sentry.replayIntegration({ maskAllText: false });',
        '// Sentry.replayIntegration(REPLAY_PRIVACY_OPTIONS) in a comment',
      ].join('\n')
    );
    const matches = RECORDER_PATTERNS.flatMap((pattern) =>
      Array.from(source.matchAll(pattern), (m) => m[0].replace(/\s+/g, ''))
    );

    expect(matches).toEqual([
      "'@statsig/session-replay'",
      'StatsigSessionReplayPlugin(',
      'replayIntegration({maskAllText:false})',
    ]);
    expect(
      matches.some((match) =>
        isApproved({ file: 'instrumentation-client.ts', match })
      )
    ).toBe(false);
  });

  it('depends on no session-recording package besides Sentry', () => {
    const pkg = JSON.parse(
      readFileSync(path.join(ROOT, 'package.json'), 'utf8')
    ) as Record<string, Record<string, string> | undefined>;
    const names = [
      ...Object.keys(pkg.dependencies ?? {}),
      ...Object.keys(pkg.devDependencies ?? {}),
      ...Object.keys(pkg.optionalDependencies ?? {}),
    ];

    expect(names.filter((name) => RECORDER_PACKAGE.test(name))).toEqual([]);
  });
});
