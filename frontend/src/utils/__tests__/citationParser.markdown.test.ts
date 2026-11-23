/**
 * Source selection must count only the citation markers the transcript
 * actually renders as badges (GOO-666 MX2-3). CitationRenderer transforms
 * Markdown text nodes after remark-gfm (and remark-math, on the math path)
 * has parsed the message, so a `[1]` inside code, math or link text never
 * becomes a badge and must not narrow the footer chips either.
 *
 * The renderer-agreement suite in
 * `components/chat/__tests__/CitationRenderer.agreement.test.tsx` pins the
 * helper to the renderer over a wide input set; these cases document the
 * individual rules.
 */
import { describe, expect, it } from 'vitest';
import {
  getReferencedCitations,
  getReferencedItemsByCitationIndex,
  getVisibleCitations,
  type Citation,
} from '@/utils/citationParser';

const CITATIONS: Citation[] = [1, 2, 3].map((n) => ({
  documentId: `doc-${n}`,
  title: `Source ${n}`,
  score: 0.5,
}));

function referenced(content: string): number[] {
  return getReferencedCitations(content, CITATIONS).map(
    (citation) => CITATIONS.indexOf(citation) + 1
  );
}

describe('markers inside code are not references', () => {
  it('ignores a marker inside a fenced block and keeps every source visible', () => {
    const content = 'Intro.\n\n```py\nvalues[1]\n```';
    expect(referenced(content)).toEqual([]);
    expect(getVisibleCitations(content, CITATIONS)).toEqual(CITATIONS);
  });

  it('ignores a marker inside an inline code span', () => {
    expect(referenced('Use `x[1]` here.')).toEqual([]);
  });

  it('ignores a marker inside a tilde fence and an unclosed fence', () => {
    expect(referenced('~~~\na[1]\n~~~\n\nSee [2].')).toEqual([2]);
    expect(referenced('See [3].\n\n```\na[1]\nstill code [2]')).toEqual([3]);
  });

  it('ignores a marker inside an indented code block', () => {
    expect(referenced('Para.\n\n    code[1]\n\nAfter [2].')).toEqual([2]);
  });

  it('still counts markers in prose around code', () => {
    expect(referenced('Claim [2].\n\n```\na[1]\n```\n\nAnd [3].')).toEqual([
      2, 3,
    ]);
  });
});

describe('cases a hand-written backtick scanner got wrong (#2290 review)', () => {
  it('does not pair stray backticks across paragraphs (F1)', () => {
    expect(referenced('Stray ` here [1].\n\nSecond ` with [2].')).toEqual([
      1, 2,
    ]);
    expect(referenced("``quoted [1]\n\nmore'' [2]")).toEqual([1, 2]);
  });

  it('treats a backtick line whose info string has a backtick as a span, not a fence (F2)', () => {
    expect(referenced('```x``` [1]\n\nmore [2]')).toEqual([1, 2]);
  });

  it('does not treat an escaped backtick as a span delimiter (F4)', () => {
    expect(referenced('esc \\` [1] ` [2] `')).toEqual([1]);
  });

  it('reads CRLF and CR-only line endings the way the renderer does', () => {
    expect(referenced('```\r\nx[1]\r\n```\r\n\r\nSee [2].')).toEqual([2]);
    expect(referenced('```\rx[1]\r```\r\rSee [2].')).toEqual([2]);
  });

  it('ends a fence at the end of its container', () => {
    expect(referenced('> ```\n> a[1]\n> ```\n\nSee [2].')).toEqual([2]);
    expect(referenced('> ```\n> a[1]\n\nAfter [2].')).toEqual([2]);
    expect(referenced('- item\n\n  ```\n  a[1]\n  ```\n- other [3]')).toEqual([
      3,
    ]);
  });
});

describe('math, links and source count', () => {
  it('ignores markers inside inline and display math', () => {
    expect(referenced('Take $a[1]$ and [2].')).toEqual([2]);
    expect(referenced('$$\nM[1]\n$$\n\nSee [3].')).toEqual([3]);
  });

  it('counts a marker next to dollar amounts that are not math delimiters', () => {
    expect(referenced('Costs $5 [1].')).toEqual([1]);
  });

  it('ignores markers inside link text, as the renderer does', () => {
    expect(referenced('[see [1]](https://example.com) and [2]')).toEqual([2]);
  });

  it('reads a marker written with a character reference', () => {
    expect(referenced('See &#91;2&#93;.')).toEqual([2]);
  });

  it('passes the source count, so an out-of-range bare group selects nothing', () => {
    expect(referenced('[2, 5]')).toEqual([]);
    expect(getVisibleCitations('[2, 5]', CITATIONS)).toEqual(CITATIONS);
    expect(getReferencedItemsByCitationIndex('[Doc 5] [1]', ['a'])).toEqual([
      'a',
    ]);
  });
});

describe('performance', () => {
  // One Markdown parse, the same one the renderer already pays for this
  // message. The bound is loose enough for a loaded CI runner and still far
  // below the 5.8 s a quadratic scanner took at this size.
  it('scans a 400 KB message dense with code and markers within 3 seconds', () => {
    const unit =
      'Claim with `a[1]` inline and [2] cited.\n\n```\nx[1]\n```\n\n- item [3]\n\n';
    const content = unit.repeat(Math.ceil(400_000 / unit.length));
    const start = performance.now();
    const result = referenced(content);
    const elapsed = performance.now() - start;

    expect(result).toEqual([2, 3]);
    expect(elapsed).toBeLessThan(3_000);
  });
});
