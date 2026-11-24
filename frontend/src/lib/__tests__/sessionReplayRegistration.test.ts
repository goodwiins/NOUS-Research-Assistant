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
import {
  mkdirSync,
  mkdtempSync,
  readdirSync,
  readFileSync,
  rmSync,
  statSync,
  writeFileSync,
} from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { describe, expect, it } from 'vitest';

const ROOT = path.resolve(__dirname, '../../..');
/**
 * Shipped source trees. `config/` holds runtime config that `next.config.js`
 * loads (GOO-795); the root's own entry points are scanned separately.
 */
const SOURCE_DIRS = ['app', 'src', 'config'];
const SOURCE_FILE = /\.(ts|tsx|js|jsx|mjs|cjs)$/;
const SKIPPED_DIRS = new Set(['node_modules', '.next', '__tests__', '__mocks__']);
const TEST_FILE = /\.(test|spec)\.[jt]sx?$/;

/**
 * Packages that record the DOM or the session, or auto-capture page URLs,
 * query params, the referrer and element text (Statsig AutoCapture, GOO-784).
 * Shared by the package.json check and the import scan; any subpath counts
 * (`@statsig/session-replay/build/index`).
 */
const RECORDER_PACKAGE_NAMES =
  '@statsig\\/session-replay|@statsig\\/web-analytics|rrweb|rrweb-snapshot|@rrweb\\/[^/\'"]+|posthog-js|logrocket|@fullstory\\/[^/\'"]+|@hotjar\\/[^/\'"]+|@openreplay\\/[^/\'"]+|@microsoft\\/clarity|smartlook-client|mouseflow[^/\'"]*|@sentry-internal\\/replay[^/\'"]*';

const RECORDER_PACKAGE = new RegExp(`^(${RECORDER_PACKAGE_NAMES})(\\/.*)?$`);

/** Ways source code can start a recorder or auto-capture. */
const RECORDER_PATTERNS: readonly RegExp[] = [
  new RegExp(`['"](${RECORDER_PACKAGE_NAMES})(\\/[^'"]*)?['"]`, 'g'),
  /\b\w*SessionReplay\w*\s*\(/g,
  /\b\w*AutoCapture\w*\s*\(/g,
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

function shippedSourceFiles(root: string = ROOT): string[] {
  const files: string[] = [];
  for (const dir of SOURCE_DIRS) walk(path.join(root, dir), files);
  // Root-level entry points: instrumentation, Sentry and Next configs.
  for (const entry of readdirSync(root)) {
    if (SOURCE_FILE.test(entry) && !TEST_FILE.test(entry)) {
      files.push(path.join(root, entry));
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

function recorderRegistrations(
  root: string = ROOT
): Array<{ file: string; match: string }> {
  const found: Array<{ file: string; match: string }> = [];
  for (const file of shippedSourceFiles(root)) {
    const source = stripComments(readFileSync(file, 'utf8'));
    for (const pattern of RECORDER_PATTERNS) {
      for (const match of source.matchAll(pattern)) {
        found.push({
          file: path.relative(root, file),
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

  it('catches deep imports and auto-capture as well as package roots', () => {
    const source = stripComments(
      [
        "import { X } from '@statsig/session-replay/build/index';",
        "import { Y } from '@statsig/web-analytics';",
        "import { record } from 'rrweb/typings/record';",
        "import { Z } from '@fullstory/browser/dist/index.js';",
        'const plugin = new StatsigAutoCapturePlugin();',
        'runStatsigAutoCapture(client);',
        "import { other } from '@statsig/react-bindings';",
      ].join('\n')
    );
    const matches = RECORDER_PATTERNS.flatMap((pattern) =>
      Array.from(source.matchAll(pattern), (m) => m[0].replace(/\s+/g, ''))
    );

    expect(matches).toEqual([
      "'@statsig/session-replay/build/index'",
      "'@statsig/web-analytics'",
      "'rrweb/typings/record'",
      "'@fullstory/browser/dist/index.js'",
      'StatsigAutoCapturePlugin(',
      'runStatsigAutoCapture(',
    ]);
    for (const name of [
      '@statsig/session-replay',
      '@statsig/session-replay/build/index',
      '@statsig/web-analytics',
      'rrweb/typings',
    ]) {
      expect(RECORDER_PACKAGE.test(name)).toBe(true);
    }
    for (const name of [
      '@statsig/react-bindings',
      '@statsig/js-client',
      '@sentry/nextjs',
      'rrweb-like',
    ]) {
      expect(RECORDER_PACKAGE.test(name)).toBe(false);
    }
  });

  it('scans the root runtime config and instrumentation files (GOO-795)', () => {
    const scanned = new Set(
      shippedSourceFiles().map((file) => path.relative(ROOT, file))
    );

    for (const file of [
      'next.config.js',
      'instrumentation.ts',
      'instrumentation-client.ts',
      'sentry.client.config.ts',
      'sentry.server.config.ts',
      'sentry.edge.config.ts',
      path.join('config', 'resolveSentryEnvironment.js'),
    ]) {
      expect(scanned.has(file), file).toBe(true);
    }
  });

  describe('in a planted frontend root (GOO-795)', () => {
    const plant = (files: Record<string, string>): string => {
      const root = mkdtempSync(path.join(tmpdir(), 'recorder-guard-'));
      for (const [file, source] of Object.entries(files)) {
        mkdirSync(path.dirname(path.join(root, file)), { recursive: true });
        writeFileSync(path.join(root, file), source);
      }
      for (const dir of SOURCE_DIRS) {
        mkdirSync(path.join(root, dir), { recursive: true });
      }
      return root;
    };
    const unapprovedIn = (
      files: Record<string, string>
    ): Array<{ file: string; match: string }> => {
      const root = plant(files);
      try {
        return recorderRegistrations(root)
          .filter((hit) => !isApproved(hit))
          .sort((a, b) => a.file.localeCompare(b.file));
      } finally {
        rmSync(root, { recursive: true, force: true });
      }
    };

    it('flags a recorder set up in a root config or instrumentation file', () => {
      expect(
        unapprovedIn({
          'next.config.mjs':
            "import { runStatsigAutoCapture } from '@statsig/web-analytics';\n",
          'instrumentation-node.ts':
            'Sentry.init({ integrations: [Sentry.replayIntegration()] });\n',
          'instrumentation-client.ts':
            'Sentry.replayIntegration({ maskAllText: false });\n',
          'config/analytics.js': 'new StatsigAutoCapturePlugin();\n',
        })
      ).toEqual([
        { file: 'config/analytics.js', match: 'StatsigAutoCapturePlugin(' },
        {
          file: 'instrumentation-client.ts',
          match: 'replayIntegration({maskAllText:false})',
        },
        { file: 'instrumentation-node.ts', match: 'replayIntegration()' },
        { file: 'next.config.mjs', match: "'@statsig/web-analytics'" },
      ]);
    });

    it('passes root files that start no recorder or only the approved one', () => {
      expect(
        unapprovedIn({
          'next.config.js': "module.exports = { output: 'standalone' };\n",
          'instrumentation.ts': 'export async function register() {}\n',
          'instrumentation-client.ts':
            'Sentry.init({ integrations: [Sentry.replayIntegration(REPLAY_PRIVACY_OPTIONS)] });\n',
          'config/resolveSentryEnvironment.js': 'module.exports = () => "dev";\n',
          'next.config.test.ts': 'new StatsigAutoCapturePlugin();\n',
        })
      ).toEqual([]);
    });
  });

  it('depends on no session-recording or auto-capture package besides Sentry', () => {
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
