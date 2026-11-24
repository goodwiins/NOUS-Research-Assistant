/**
 * The footer chips and the thread citation list choose their sources with
 * `getReferencedCitations`; the transcript draws badges with
 * `CitationRenderer`. When the two disagree, a badge renders with its source
 * missing from the chips, or the chips narrow to a source with no badge
 * (GOO-666 MX2-3). This suite renders every input through the real renderer,
 * including the lazy math path, and requires the helper to select exactly the
 * in-range indices that rendered as badges.
 */
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import { act, cleanup, render } from '@testing-library/react';
import { CitationRenderer } from '../CitationRenderer';
import { getReferencedCitations, type Citation } from '@/utils/citationParser';

vi.mock('next/dynamic', () => ({
  default: () => {
    const MockHighlighter = ({
      children,
    }: {
      children?: React.ReactNode;
    }): React.ReactElement => (
      <pre>
        <code>{children}</code>
      </pre>
    );
    return MockHighlighter;
  },
}));

const CITATIONS: Citation[] = [1, 2, 3].map((n) => ({
  documentId: `doc-${n}`,
  title: `Paper ${n}`,
  score: 0.5,
}));

const INPUTS: string[] = [
  // Prose only
  'Plain claim [1].',
  'Two claims [1] and [3].',
  'Grouped [1, 2].',
  'Labelled [Doc 2] and [Source 3].',
  'Chained [1][2].',
  'Out of range [7].',
  'Labelled out of range [Doc 9] and [1].',
  'Bare group partly out of range [2, 5].',
  'Index expression arr[1] then [2].',
  'Matrix m[1][2] and [3].',
  'Year [2023] and [1].',
  'Zero [0] and [2].',
  '**Bold [1]** and _em [2]_.',
  '# Heading [1]\n\nBody [2].',
  '- item [1]\n- item [2]\n- item [3]',
  '1. first [1]\n2. second [3]',
  '> quoted [2]\n\nAfter [1].',
  '| A | B |\n| --- | --- |\n| x [1] | y [2] |',
  '- [ ] task [1]\n- [x] done [3]',
  '~~struck [2]~~ and [1].',
  'Footnote[^1] and [2].\n\n[^1]: Note [3].',
  // Fenced code
  'Intro.\n\n```py\nvalues[1]\n```',
  'Claim [2].\n\n```\na[1]\n```\n\nAnd [3].',
  '~~~\na[1]\n~~~\n\nSee [2].',
  'See [3].\n\n```\na[1]\nstill code [2]',
  '````\n```\nx[1]\n```\n````\n\nOut [2].',
  '```\nx[1]\n~~~\ny[2]\n```\n\nOut [3].',
  '```\nx[1]\n```python\ny[2]\n```\n\nOut [3].',
  '   ```\n   x[1]\n   ```\n\nOut [2].',
  '```x``` [1]\n\nmore [2]',
  '``` a ` b\nc [1]\n```',
  '~~~ a ` b\nc [1]\n~~~\n\nd [2]',
  // Inline code
  'Use `x[1]` here.',
  'Mixed `a` [1] `b[2]` [3].',
  'Double ``a ` [1]`` and [2].',
  'Run length ``` `` [1] ``` and [2].',
  '`x`[1] then [2].',
  'Unmatched ` tick [1] and [2].',
  'Stray ` here [1].\n\nSecond ` with [2].',
  "``quoted [1]\n\nmore'' [2]",
  '- a ` [1]\n- b ` [2]',
  '| `a | b` |\n| --- | --- |\n| [1] | [2] |',
  '# Head ` [1]\n\nPara ` [2]',
  'esc \\` [1] ` [2] `',
  '\\`not code [1]\\` and [2]',
  // Line endings and containers
  '```\r\nx[1]\r\n```\r\n\r\nSee [2].',
  '```\rx[1]\r```\r\rSee [2].',
  'Line one [1]\r\nline two `[2]`\r\n',
  '> ```\n> a[1]\n> ```\n\nSee [2].',
  '> ```\n> a[1]\n\nAfter [2].',
  '- item\n\n  ```\n  a[1]\n  ```\n- other [3]',
  '- ```\n  a[1]\n- b [2]',
  // Indented code
  'Para.\n\n    code[1]\n\nAfter [2].',
  '- list\n\n    continuation [1]',
  // Math
  'Take $a[1]$ and [2].',
  '$$\nM[1]\n$$\n\nSee [3].',
  'Costs $5 [1].',
  'From $5 to $10 [1] range.',
  'Escaped \\$x[1]\\$ and [2].',
  'Inline \\(x[1]\\) and [2].',
  'Display \\[x[1]\\] and [3].',
  'Math in code `$a[1]$` and [2].',
  'Relation $E=mc^2$ [Doc 1].',
  'Split $a\nb[1]$ math and [2].',
  'Display $$\nM[1]\n$$ inline [3].',
  // Links, references, HTML and escapes
  '[see [1]](https://example.com) and [2]',
  '[1](https://example.com) and [2].',
  'Autolink <https://x.com/[1]> and [2].',
  'URL https://x.com/a[1] and [3].',
  '[ref][1] and [2].\n\n[1]: https://example.com',
  '[1]: https://example.com\n\nText [2].',
  '[see [1]][r] and [2].\n\n[r]: https://example.com',
  '[see [3]] unresolved and [2].',
  '<span>[1]</span> and [2].',
  '<!-- [1] -->\n\nText [2].',
  '<div>\n[1]\n</div>\n\nText [3].',
  'Escaped \\[1\\] and [2].',
  'Reference &#91;2&#93; and [1].',
  '![alt [1]](img.png) and [2].',
];

function renderedIndices(container: HTMLElement): number[] {
  const indices = Array.from(container.querySelectorAll('button'))
    .map((button) => button.getAttribute('aria-label') ?? '')
    .map((label) => /^Source (\d+)/.exec(label))
    .filter((match): match is RegExpExecArray => match !== null)
    .map((match) => Number(match[1]))
    .filter((index) => index >= 1 && index <= CITATIONS.length);
  return Array.from(new Set(indices)).sort((a, b) => a - b);
}

function helperIndices(content: string): number[] {
  return getReferencedCitations(content, CITATIONS)
    .map((citation) => CITATIONS.indexOf(citation) + 1)
    .sort((a, b) => a - b);
}

/** Let the lazily loaded math renderer replace its plain fallback. */
async function settle(): Promise<void> {
  for (let i = 0; i < 3; i += 1) {
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
  }
}

beforeAll(async () => {
  await import('../ChatMarkdownMath');
});

afterEach(cleanup);

describe('getReferencedCitations agrees with CitationRenderer', () => {
  it('covers at least 69 inputs', () => {
    expect(INPUTS.length).toBeGreaterThanOrEqual(69);
    expect(new Set(INPUTS).size).toBe(INPUTS.length);
  });

  it('reaches the math path before comparing', async () => {
    const { container } = render(
      <CitationRenderer
        content={'Take $a[1]$ and [2].'}
        citations={CITATIONS}
      />
    );
    await settle();

    expect(container.querySelector('.katex')).not.toBeNull();
    expect(renderedIndices(container)).toEqual([2]);
  });

  // A plain loop rather than it.each: each title is the input itself, and
  // it.each would format the `$` in the math inputs.
  for (const input of INPUTS) {
    it(JSON.stringify(input), async () => {
      const { container } = render(
        <CitationRenderer content={input} citations={CITATIONS} />
      );
      await settle();

      expect(helperIndices(input)).toEqual(renderedIndices(container));
    });
  }
});
