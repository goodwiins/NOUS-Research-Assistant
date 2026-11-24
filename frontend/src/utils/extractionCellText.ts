import type { ExtractionCellValue } from '@/types/scispace';

/**
 * The text of one extraction-matrix cell, for the grid and the CSV export
 * (GOO-763). Arrays and objects are shown as JSON, never `[object Object]`.
 */
export function extractionCellText(
  value: ExtractionCellValue | undefined
): string {
  if (value === null || value === undefined) return '';
  if (typeof value === 'object') return JSON.stringify(value);
  return String(value);
}
