/**
 * GOO-724 guard: no `Math.random()` may reach a displayed value.
 *
 * Quality cards, dashboards and the usage heatmap rendered `Math.random()`
 * numbers as if they were measurements. This scans every production source
 * file under `src/` and `app/` and fails on any `Math.random()` that is not
 * one of:
 *
 * - an id or key suffix (`Math.random().toString(36)`), or
 * - a file in ALLOWED below, each with the reason it never renders a metric.
 *
 * Adding a file to ALLOWED needs the same justification. When a metric has
 * no measurement, show an explicit "unavailable" state instead.
 */
import { readFileSync, readdirSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { describe, expect, it } from 'vitest';

const HERE = dirname(fileURLToPath(import.meta.url));
// frontend/src/test/architecture/__tests__ -> frontend (4 levels up)
const FRONTEND_ROOT = resolve(HERE, '../../../..');

const ALLOWED: Record<string, string> = {
  'src/components/ui/sidebar.tsx': 'skeleton placeholder width',
  'src/components/ui/agent-plan.tsx': 'demo-only status toggle, no metric',
  'src/components/citations/CitationGraph.tsx': 'node layout shuffle',
  'src/components/citations/CitationGraphSkeleton.tsx':
    'skeleton placeholder size',
  // Fabricated search results/click rates; removed by #2248 (GOO-649),
  // which owns this file. Drop this entry once that lands.
  'src/services/documentAnalyticsApi.ts': 'owned by #2248 (GOO-649)',
};

const isTestFile = (path: string): boolean =>
  /(^|\/)(__tests__|__mocks__|test|mocks)\//.test(path) ||
  /\.(test|spec|stories)\.tsx?$/.test(path);

function walk(relDir: string): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(resolve(FRONTEND_ROOT, relDir), {
    withFileTypes: true,
  })) {
    const rel = `${relDir}/${entry.name}`;
    if (entry.isDirectory()) {
      if (entry.name !== 'generated') out.push(...walk(rel));
    } else if (/\.tsx?$/.test(entry.name) && !entry.name.endsWith('.d.ts')) {
      out.push(rel);
    }
  }
  return out;
}

/** `Math.random()` calls that are not an id suffix, as `file:line` strings. */
export function findRandomValues(path: string, source: string): string[] {
  const hits: string[] = [];
  source.split('\n').forEach((line, index) => {
    // Comments that explain a removed call are not calls.
    if (/^\s*(\/\/|\/\*|\*)/.test(line)) return;
    const calls = line.match(/Math\.random\(\)/g)?.length ?? 0;
    const idCalls = line.match(/Math\.random\(\)\.toString\(36\)/g)?.length ?? 0;
    if (calls > idCalls) hits.push(`${path}:${index + 1}`);
  });
  return hits;
}

describe('no Math.random() reaches a displayed value (GOO-724)', () => {
  it('finds no unexplained Math.random() in production source', () => {
    const files = [...walk('src'), ...walk('app')].filter(
      (path) => !isTestFile(path) && !(path in ALLOWED)
    );
    const hits = files.flatMap((path) =>
      findRandomValues(path, readFileSync(resolve(FRONTEND_ROOT, path), 'utf8'))
    );
    expect(hits).toEqual([]);
  });

  it('flags a random metric but not an id suffix', () => {
    expect(
      findRandomValues('x.ts', 'const latency = 300 + Math.random() * 1200;')
    ).toEqual(['x.ts:1']);
    expect(
      findRandomValues('x.ts', 'const id = `w_${Math.random().toString(36)}`;')
    ).toEqual([]);
    expect(
      findRandomValues(
        'x.ts',
        'const v = Math.random().toString(36) + Math.random() * 2;'
      )
    ).toEqual(['x.ts:1']);
    expect(
      findRandomValues('x.ts', '  // it used to be Math.random() here')
    ).toEqual([]);
  });
});
