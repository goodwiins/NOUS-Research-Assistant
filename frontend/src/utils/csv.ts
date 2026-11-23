/**
 * CSV for files and clipboard text a user opens in a spreadsheet
 * (GOO-666 FR-15).
 *
 * A spreadsheet runs a cell that starts with `=`, `+`, `-` or `@` as a
 * formula, and Excel and others also trim leading whitespace (spaces, tab,
 * CR, LF) before deciding. Such a string is prefixed with `'` so it stays
 * literal text: the same rule as the backend exports (GOO-691,
 * `backend/src/api/quality/user_behavior.py` `_csv_cell`), widened to the
 * leading-whitespace forms. Only strings are prefixed, so a negative number
 * stays a number.
 */

const FORMULA_START = /^[\s]*[=+\-@]|^[\t\r]/;
const NEEDS_QUOTES = /[",\r\n]/;

/** Make user-controlled text literal in a spreadsheet; numbers pass. */
export function neutralizeCsvFormula(value: unknown): string {
  if (value === null || value === undefined) return '';
  if (typeof value !== 'string') return String(value);
  return FORMULA_START.test(value) ? `'${value}` : value;
}

/** One CSV field: neutralised, then quoted when it holds `"`, `,` or a newline. */
export function csvCell(value: unknown): string {
  const text = neutralizeCsvFormula(value);
  return NEEDS_QUOTES.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
}

/** Rows of cells as CSV text, `\n` between rows. */
export function toCsv(rows: ReadonlyArray<ReadonlyArray<unknown>>): string {
  return rows.map((row) => row.map(csvCell).join(',')).join('\n');
}
