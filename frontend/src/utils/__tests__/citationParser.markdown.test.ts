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

  it('re-reads cached content when the source count grows', () => {
    // The parse cache is keyed by content AND count: with two sources the
    // bare group [1, 3] is out of range and selects nothing; once a third
    // source arrives the same committed text selects the first and third.
    const content = 'Count-keyed cache check [1, 3].';

    expect(getReferencedItemsByCitationIndex(content, ['a', 'b'])).toEqual([]);
    expect(
      getReferencedItemsByCitationIndex(content, ['a', 'b', 'c'])
    ).toEqual(['a', 'c']);
  });
});

describe('performance', () => {
  // One Markdown parse, the same one the renderer already pays for this
  // message. A wall-clock bound flakes on loaded CI runners (400 KB took
  // 4.2-4.6 s there, 0.9 s locally), so this pins the growth instead. 16x the
  // input costs 16-21x the time, quiet or under 4 parallel coverage runs; a
  // quadratic scanner costs about 256x (a mutant with an O(n^2) pre-scan
  // measured over 60x). The bound sits between. Small and large runs
  // alternate, so a load spike lands on both sizes rather than on one.
  // The input is block-structured, as real answers are; micromark itself is
  // superlinear on one giant paragraph.
  const unit =
    'Claim with `a[1]` inline and [2] cited.\n\n```\nx[1]\n```\n\n- item [3]\n\n';
  const message = (bytes: number): string =>
    unit.repeat(Math.ceil(bytes / unit.length));

  // Results are cached per content, so every timed run gets its own text.
  let variant = 0;
  const fresh = (content: string): string =>
    `Run ${(variant += 1)}.\n\n${content}`;

  function parseMs(content: string): number {
    const start = performance.now();
    expect(referenced(content)).toEqual([2, 3]);
    return performance.now() - start;
  }

  const median = (values: number[]): number =>
    [...values].sort((a, b) => a - b)[Math.floor(values.length / 2)];

  it('scales linearly with message size on code- and marker-dense input', () => {
    referenced(fresh(message(20_000))); // warm the parser before timing
    const small: number[] = [];
    const large: number[] = [];
    for (let pair = 0; pair < 5; pair += 1) {
      small.push(parseMs(fresh(message(12_500))));
      large.push(parseMs(fresh(message(200_000))));
    }

    expect(median(large) / median(small)).toBeLessThan(40);
  }, 60_000);

  it('parses a committed message once, however often it is asked again', () => {
    const content = fresh(message(200_000));
    const first = parseMs(content);
    // A store update rebuilds the citation array; the content is unchanged.
    const again = parseMs(content.slice());

    expect(again).toBeLessThan(first / 10);
  });

  it('keeps the 128 most recently used messages and drops older ones', () => {
    // Fill the cache, so each new entry below evicts exactly one old one.
    for (let other = 0; other < 128; other += 1) referenced(fresh('[1]'));
    const kept = fresh(message(50_000));
    const dropped = fresh(message(50_000));
    const keptFirst = parseMs(kept);
    const droppedFirst = parseMs(dropped);
    // `kept` is used again, so `dropped` is now the oldest entry.
    parseMs(kept);
    for (let other = 0; other < 127; other += 1) referenced(fresh('[1]'));

    expect(parseMs(kept)).toBeLessThan(keptFirst / 10);
    expect(parseMs(dropped)).toBeGreaterThan(droppedFirst / 10);
  });

  it('skips the parse when there are no sources to select from', () => {
    const content = fresh(message(200_000));
    const start = performance.now();
    expect(getReferencedItemsByCitationIndex(content, [])).toEqual([]);
    const empty = performance.now() - start;

    expect(empty).toBeLessThan(parseMs(content) / 10);
  });
});
