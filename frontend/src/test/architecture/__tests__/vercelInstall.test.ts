/**
 * GOO-510: every Vercel config installs from the committed lockfile, so a
 * deploy resolves the same dependency versions CI tested. The root
 * `pnpm-lock.yaml` is the only JS lockfile (docs/engineering/frontend.md).
 */
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { describe, expect, it } from 'vitest';

const HERE = dirname(fileURLToPath(import.meta.url));
// frontend/src/test/architecture/__tests__ -> repo root (5 levels up)
const REPO_ROOT = resolve(HERE, '../../../../..');

describe.each(['vercel.json', 'frontend/vercel.json'])('%s', (relPath) => {
  const config = JSON.parse(
    readFileSync(resolve(REPO_ROOT, relPath), 'utf-8')
  ) as { installCommand?: string };

  it('installs with a frozen lockfile', () => {
    expect(config.installCommand).toMatch(/pnpm install --frozen-lockfile\b/);
    expect(config.installCommand).not.toContain('--no-frozen-lockfile');
  });
});
