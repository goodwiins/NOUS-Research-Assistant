/**
 * GOO-666 FR-15: an extracted table's CSV (copy and download) neutralises
 * cells that a spreadsheet would run as formulas, and quotes commas.
 */
import { fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ExtractedTablePreview } from '../ExtractedTablePreview';

const table = {
  page: 2,
  table_index: 0,
  method: 'pdfplumber',
  markdown: '',
  rows: [
    ['Name', 'Value'],
    ['=HYPERLINK("http://x.test")', '-3'],
    [' @SUM(A1)', 'a, b'],
  ],
};
const EXPECTED = `Name,Value\n"'=HYPERLINK(""http://x.test"")",'-3\n' @SUM(A1),"a, b"`;

afterEach(() => {
  vi.restoreAllMocks();
});

describe('ExtractedTablePreview CSV', () => {
  it('copies neutralised, quoted CSV', () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
    render(<ExtractedTablePreview documentId="d1" table={table} onClose={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: /copy csv/i }));
    expect(writeText).toHaveBeenCalledWith(EXPECTED);
  });

  it('downloads the same CSV', async () => {
    let blob: Blob | undefined;
    URL.createObjectURL = vi.fn((b: Blob) => {
      blob = b;
      return 'blob:x';
    }) as typeof URL.createObjectURL;
    URL.revokeObjectURL = vi.fn();
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);
    render(<ExtractedTablePreview documentId="d1" table={table} onClose={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: /download csv/i }));
    // jsdom's Blob has no text(): read it the browser way.
    const text = await new Promise<string>((done) => {
      const reader = new FileReader();
      reader.onload = () => done(String(reader.result));
      reader.readAsText(blob as Blob);
    });
    expect(text).toBe(EXPECTED);
  });
});
