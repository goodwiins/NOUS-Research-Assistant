/**
 * GOO-666 FR-15: CSV exports must not let a cell run as a spreadsheet
 * formula. Same rule as the backend (GOO-691), plus leading whitespace.
 */
import { describe, expect, it } from 'vitest';
import { csvCell, neutralizeCsvFormula, toCsv } from '../csv';

describe('neutralizeCsvFormula', () => {
  it.each([
    ['=HYPERLINK("http://x.test","click")'],
    ['+1+cmd|"/c calc"!A0'],
    ['-2+3'],
    ['@SUM(A1:A2)'],
    ['\tTAB=1'],
    ['\rCR=1'],
    [' =1+1'],
    ['   @SUM(A1)'],
    ['\n=1+1'],
    [' \t -1'],
  ])('prefixes %j', (value) => {
    expect(neutralizeCsvFormula(value)).toBe(`'${value}`);
  });

  it.each(['report.pdf', 'a=b', 'x+y', 'user@example.test', '', ' plain'])(
    'leaves %j alone',
    (value) => {
      expect(neutralizeCsvFormula(value)).toBe(value);
    }
  );

  it.each(['-5', '-3.2', '+1.5', '-12%', '42', '0.5'])(
    'leaves the plain number %j as it is',
    (value) => {
      expect(neutralizeCsvFormula(value)).toBe(value);
    }
  );

  it.each([
    '-1e5',
    '-5.',
    '-.5',
    '--5',
    '-5%%',
    '-5,000',
    '- 5',
    ' -5',
    '-5\n=cmd',
    '+1+1',
  ])('still prefixes %j, which is not strictly a plain number', (value) => {
    expect(neutralizeCsvFormula(value)).toBe(`'${value}`);
  });

  it('keeps numbers (including negative ones) and booleans as they are', () => {
    expect(neutralizeCsvFormula(-5)).toBe('-5');
    // Non-strings are never prefixed, even when their text is not a plain
    // number.
    expect(neutralizeCsvFormula(-1e21)).toBe('-1e+21');
    expect(neutralizeCsvFormula(Number.NEGATIVE_INFINITY)).toBe('-Infinity');
    expect(neutralizeCsvFormula(1024)).toBe('1024');
    expect(neutralizeCsvFormula(true)).toBe('true');
    expect(neutralizeCsvFormula(null)).toBe('');
    expect(neutralizeCsvFormula(undefined)).toBe('');
  });
});

describe('csvCell / toCsv', () => {
  it('quotes cells with commas, quotes or newlines, after neutralising', () => {
    expect(csvCell('a,b')).toBe('"a,b"');
    expect(csvCell('say "hi"')).toBe('"say ""hi"""');
    expect(csvCell('=1,2')).toBe(`"'=1,2"`);
    expect(csvCell('line\nbreak')).toBe('"line\nbreak"');
  });

  it('builds rows, keeping numeric columns numeric', () => {
    expect(
      toCsv([
        ['Filename', 'Size'],
        ['=cmd.pdf', -1],
        ['notes, v2.txt', 20],
      ])
    ).toBe(`Filename,Size\n'=cmd.pdf,-1\n"notes, v2.txt",20`);
  });
});
