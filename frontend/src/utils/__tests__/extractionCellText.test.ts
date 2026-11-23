/**
 * GOO-763: GET /matrices/{id} is an untyped dict, so a cell value can be any
 * JSON value. It is turned into text once, here, for the grid and the CSV.
 */
import { describe, expect, it } from 'vitest';
import { extractionCellText } from '../extractionCellText';

describe('extractionCellText', () => {
  it('shows an empty cell as nothing', () => {
    expect(extractionCellText(null)).toBe('');
    expect(extractionCellText(undefined)).toBe('');
  });

  it('keeps strings, numbers and booleans as their text', () => {
    expect(extractionCellText('=cmd')).toBe('=cmd');
    expect(extractionCellText(-5)).toBe('-5');
    expect(extractionCellText(0)).toBe('0');
    expect(extractionCellText(false)).toBe('false');
  });

  it('shows arrays and objects as JSON, never [object Object]', () => {
    expect(extractionCellText(['a', 2])).toBe('["a",2]');
    expect(extractionCellText({ n: 120 })).toBe('{"n":120}');
  });
});
