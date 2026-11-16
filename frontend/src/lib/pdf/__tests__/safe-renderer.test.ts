import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { describe, expect, it, vi } from 'vitest';
import { BoundedPdfCanvasFactory } from '../safe-renderer';

/** Every method pdf.js calls on the CanvasFactory we hand it. */
function canvasFactoryMethodsUsedByPdfJs(): string[] {
  const require = createRequire(import.meta.url);
  const source = readFileSync(
    require.resolve('pdfjs-dist/build/pdf.mjs'),
    'utf8'
  );
  const names = new Set<string>();
  for (const match of source.matchAll(/canvasFactory\.(\w+)\(/gi))
    names.add(match[1]);
  return [...names].sort();
}

describe('BoundedPdfCanvasFactory', () => {
  it('implements every method the installed pdf.js calls on its canvas factory', () => {
    const used = canvasFactoryMethodsUsedByPdfJs();
    expect(used).toContain('create');
    const factory = new BoundedPdfCanvasFactory() as unknown as Record<
      string,
      unknown
    >;
    const missing = used.filter((name) => typeof factory[name] !== 'function');
    expect(missing).toEqual([]);
  });

  it('gives pdf.js an opaque 2d context for the page canvas', () => {
    const context = {} as CanvasRenderingContext2D;
    const canvas = document.createElement('canvas');
    const getContext = vi
      .spyOn(canvas, 'getContext')
      .mockReturnValue(context as never);

    expect(new BoundedPdfCanvasFactory().getNoAlphaContext(canvas)).toBe(
      context
    );
    expect(getContext).toHaveBeenCalledWith('2d', { alpha: false });
  });

  it('fails clearly when the browser has no 2d canvas', () => {
    const canvas = document.createElement('canvas');
    vi.spyOn(canvas, 'getContext').mockReturnValue(null);
    expect(() =>
      new BoundedPdfCanvasFactory().getNoAlphaContext(canvas)
    ).toThrow('Canvas is unavailable');
  });
});
