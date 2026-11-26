/**
 * The root layout ships on every route, /login included. The Markdown parser
 * stack (unified, remark, micromark) is about 33 KB gzip and is only needed
 * where messages render, so it must never be reachable from the root layout
 * through static imports. It once was: app/providers.tsx imported AuthProvider
 * from the `@/hooks` barrel, and the barrel re-exported useCitationsForThread,
 * which imports citationParser (GOO-666 MX2-3).
 *
 * This walks the static import graph (dynamic `import()` is a lazy chunk and
 * is not followed; `import type` is erased and is skipped).
 */
import { existsSync, readFileSync, statSync } from 'node:fs';
import path from 'node:path';
import { describe, expect, it } from 'vitest';

const ROOT = path.resolve(__dirname, '../..');
const EXTENSIONS = ['.ts', '.tsx', '.js', '.jsx', '.mjs'];

const PARSER_PACKAGE =
  /^(unified|remark-parse|remark-gfm|remark-math|micromark|mdast-util-from-markdown)(\/|$)/;

function resolveFile(base: string): string | null {
  const candidates = [
    base,
    ...EXTENSIONS.map((ext) => base + ext),
    ...EXTENSIONS.map((ext) => path.join(base, 'index' + ext)),
  ];
  return (
    candidates.find((file) => existsSync(file) && statSync(file).isFile()) ??
    null
  );
}

// Mirrors the `paths` table in tsconfig.json that these modules use.
function resolveSpecifier(specifier: string, from: string): string | null {
  if (specifier.startsWith('.')) {
    return resolveFile(path.resolve(path.dirname(from), specifier));
  }
  if (specifier === '@/nous') {
    return resolveFile(path.join(ROOT, 'app/components/nous/index.ts'));
  }
  if (specifier.startsWith('@/nous/')) {
    return resolveFile(
      path.join(ROOT, 'app/components/nous', specifier.slice(7))
    );
  }
  if (specifier.startsWith('@/')) {
    const rest = specifier.slice(2);
    return (
      resolveFile(path.join(ROOT, 'src', rest)) ??
      resolveFile(path.join(ROOT, 'app', rest))
    );
  }
  return null; // a package
}

function staticImports(source: string): string[] {
  const specifiers: string[] = [];
  const fromClause =
    /^\s*(?:import|export)\s+(?!type\s)[^'";]*?\sfrom\s+['"]([^'"]+)['"]/gm;
  const sideEffect = /^\s*import\s+['"]([^'"]+)['"]/gm;
  for (const pattern of [fromClause, sideEffect]) {
    for (const match of source.matchAll(pattern)) specifiers.push(match[1]);
  }
  return specifiers;
}

/** Every module and package the entry reaches through static imports. */
function eagerGraph(entry: string): {
  files: Set<string>;
  packages: Set<string>;
} {
  const files = new Set<string>();
  const packages = new Set<string>();
  const queue = [path.join(ROOT, entry)];
  while (queue.length > 0) {
    const file = queue.pop()!;
    if (files.has(file)) continue;
    files.add(file);
    for (const specifier of staticImports(readFileSync(file, 'utf8'))) {
      const resolved = resolveSpecifier(specifier, file);
      if (resolved) queue.push(resolved);
      else if (!specifier.startsWith('.') && !specifier.startsWith('@/'))
        packages.add(specifier);
    }
  }
  return { files, packages };
}

describe('root layout import graph', () => {
  const { files, packages } = eagerGraph('app/layout.tsx');

  it('walks the providers the layout mounts', () => {
    expect(files).toContain(path.join(ROOT, 'app/providers.tsx'));
    expect(files).toContain(path.join(ROOT, 'src/hooks/useAuth.tsx'));
  });

  it('does not reach the Markdown parser stack', () => {
    expect([...packages].filter((name) => PARSER_PACKAGE.test(name))).toEqual(
      []
    );
    expect(files).not.toContain(path.join(ROOT, 'src/utils/citationParser.ts'));
  });
});
