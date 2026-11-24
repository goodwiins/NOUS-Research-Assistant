/**
 * Citation Parser Utility
 *
 * Parses inline citation references like [Doc 1], [Doc 2] from AI response text
 * and provides utilities for rendering with interactive citation links.
 */

import { unified } from 'unified';
import remarkParse from 'remark-parse';
import remarkGfm from 'remark-gfm';
import remarkMath from 'remark-math';
import { MATH_DELIMITERS } from '@/lib/markdown-utils';

export interface Citation {
  documentId?: string; // Optional: may be undefined for external references
  externalReferenceId?: string; // For non-database references (e.g., arXiv IDs)
  /** Canonical one-based `[Doc N]` position retained across persistence. */
  sourcePosition?: number;
  chunkId?: string;
  chunkIndex?: number;
  title: string;
  score: number;
  content?: string;
  source?: string;
  /** Page the cited passage sits on, when the backend knows it. Renders as the
   * `p. N` locator in the sources list; absent means no locator, never a
   * guessed one. */
  pageNumber?: number;
}

/**
 * Check if citation has a navigable document (database reference)
 */
export function isNavigableCitation(citation: Citation): boolean {
  return !!citation.documentId;
}

/**
 * Get the display identifier for a citation
 */
export function getCitationIdentifier(citation: Citation): string {
  return citation.documentId || citation.externalReferenceId || 'unknown';
}

export interface ParsedSegment {
  type: 'text' | 'citation';
  content: string;
  citationIndex?: number; // 1-based index matching [Doc N]
}

/**
 * Matches any bracketed section so we can parse both single and grouped
 * citation styles such as:
 * - [Doc 1]
 * - [1]
 * - [Doc 2, Doc 3, Doc 4]
 */
const BRACKET_GROUP_PATTERN = /\[([^\]]+)\]/g;
/** Bare `[12]` is only a citation when nothing before the bracket makes it an
 * index expression (`arr[0]`, `matrix[1][2]`) and nothing after it makes it a
 * markdown link or link definition (`[1](url)`, `[1]: url`).
 *
 * Only a closing bracket or paren counts as that prefix: an identifier in
 * front is ambiguous (`arr[1]` vs the compact citation style `claim[1]`), and
 * the resolvability check is the real discriminator there. */
const INDEX_EXPRESSION_PREFIX = /[\])]$/;
/** Bare numbers above this are years, quantities and identifiers far more
 * often than citation indices; an explicit `Doc`/`Source`/`Ref` prefix is
 * always honoured regardless. */
const MAX_BARE_CITATION_INDEX = 99;
const CITATION_LABEL_PATTERN = /(Doc|Source|Ref)/i;
const CITATION_ITEM_PATTERN =
  /(?:(Doc|Source|Ref)[\s\u00a0\u2002\u2003]*)?(\d+)/gi;
const GROUP_DELIMITER_PATTERN = /^[,\s\u00a0\u2002\u2003]*$/;

function parseCitationGroup(
  content: string,
  maxIndex: number
): number[] | null {
  const indices: number[] = [];
  let sawLabel = false;
  let lastIndex = 0;
  let match: RegExpExecArray | null;

  CITATION_ITEM_PATTERN.lastIndex = 0;

  while ((match = CITATION_ITEM_PATTERN.exec(content)) !== null) {
    const between = content.slice(lastIndex, match.index);
    if (!GROUP_DELIMITER_PATTERN.test(between)) {
      return null;
    }

    if (match[1]) sawLabel = true;
    indices.push(parseInt(match[2], 10));
    lastIndex = match.index + match[0].length;
  }

  if (indices.length === 0) {
    return null;
  }

  const trailing = content.slice(lastIndex);
  if (!GROUP_DELIMITER_PATTERN.test(trailing)) {
    return null;
  }

  // An unlabelled group has to look like a citation index to be treated as
  // one: `[0]` and `[2023]` are almost always something else.
  if (!sawLabel && indices.some((index) => index < 1 || index > maxIndex)) {
    return null;
  }

  return indices;
}

/**
 * Parse message content into segments of text and citations
 *
 * @param content - The raw message content from AI response
 * @returns Array of parsed segments with text and citation references
 *
 * @example
 * const segments = parseMessageWithCitations("Based on [Doc 1], the answer is...");
 * // Returns: [
 * //   { type: 'text', content: 'Based on ' },
 * //   { type: 'citation', content: '[Doc 1]', citationIndex: 1 },
 * //   { type: 'text', content: ', the answer is...' }
 * // ]
 */
export function parseMessageWithCitations(
  content: string,
  options: {
    /** Highest index the caller can actually resolve to a source. Bare
     * bracketed numbers above it stay plain text instead of rendering as an
     * inert badge (or, worse, linking to the wrong document). */
    citationCount?: number;
  } = {}
): ParsedSegment[] {
  const maxIndex = Math.min(
    options.citationCount ?? MAX_BARE_CITATION_INDEX,
    MAX_BARE_CITATION_INDEX
  );
  const segments: ParsedSegment[] = [];
  let lastIndex = 0;
  let match: RegExpExecArray | null;

  BRACKET_GROUP_PATTERN.lastIndex = 0;
  let previousCitationEnd = -1;

  while ((match = BRACKET_GROUP_PATTERN.exec(content)) !== null) {
    if (match.index > lastIndex) {
      segments.push({
        type: 'text',
        content: content.slice(lastIndex, match.index),
      });
    }

    const groupContent = match[1];
    // A labelled group ([Doc 1], [Source 2]) is unambiguous; only bare numeric
    // groups need the surrounding syntax checks.
    const isLabelled = CITATION_LABEL_PATTERN.test(groupContent);
    // `[1][2]` is a chained citation, not indexing: the `]` in front only
    // disqualifies the group when it does NOT close a citation of its own.
    const precededByIndexExpression =
      !isLabelled &&
      !(previousCitationEnd === match.index) &&
      INDEX_EXPRESSION_PREFIX.test(content.slice(0, match.index));
    const nextChar = content.charAt(match.index + match[0].length);
    const isMarkdownLink =
      !isLabelled && (nextChar === '(' || nextChar === ':');
    const citationIndices =
      precededByIndexExpression || isMarkdownLink
        ? null
        : parseCitationGroup(groupContent, maxIndex);

    if (!citationIndices) {
      segments.push({
        type: 'text',
        content: match[0],
      });
    } else {
      citationIndices.forEach((citationIndex, index) => {
        segments.push({
          type: 'citation',
          content: `[Doc ${citationIndex}]`,
          citationIndex,
        });

        if (index < citationIndices.length - 1) {
          segments.push({
            type: 'text',
            content: ', ',
          });
        }
      });
    }

    lastIndex = match.index + match[0].length;
    if (citationIndices) previousCitationEnd = lastIndex;
  }

  // Add remaining text after last citation
  if (lastIndex < content.length) {
    segments.push({
      type: 'text',
      content: content.slice(lastIndex),
    });
  }

  return segments;
}

/**
 * Check if message content contains any citation references
 *
 * @param content - The message content to check
 * @returns True if content contains citation patterns
 */
export function hasCitations(
  content: string,
  options: { citationCount?: number } = {}
): boolean {
  return extractCitationIndices(content, options).length > 0;
}

/**
 * Extract all unique citation indices from content
 *
 * @param content - The message content to scan
 * @returns Array of unique citation indices (1-based)
 */
export function extractCitationIndices(
  content: string,
  options: { citationCount?: number } = {}
): number[] {
  const indices = parseMessageWithCitations(content, options)
    .filter((segment) => segment.type === 'citation')
    .map((segment) => segment.citationIndex)
    .filter((index): index is number => typeof index === 'number');

  return Array.from(new Set(indices)).sort((a, b) => a - b);
}

/**
 * The parsers ChatMarkdown renders with: remark-gfm always, plus remark-math
 * on the path it takes when `MATH_DELIMITERS` matches. Parsing only (no
 * rehype), so neither carries KaTeX.
 */
const plainMarkdownParser = unified().use(remarkParse).use(remarkGfm).freeze();
const mathMarkdownParser = unified()
  .use(remarkParse)
  .use(remarkGfm)
  .use(remarkMath)
  .freeze();

type MarkdownNode = {
  type: string;
  value?: string;
  children?: MarkdownNode[];
};

// Enough for every committed message of a long thread across its consumers
// (transcript bubbles and the context rail) without holding unbounded text.
const RENDERED_INDEX_CACHE_LIMIT = 128;
const renderedIndexCache = new Map<string, Map<number, readonly number[]>>();

/**
 * Citation indices CitationRenderer draws as badges for this content.
 *
 * The renderer parses the message with ChatMarkdown's plugins and runs
 * `parseMessageWithCitations` over each `text` node outside links, so markers
 * inside `code`, `inlineCode`, `math` and `inlineMath` (value-only nodes),
 * link text, URLs and HTML never become badges. Walking the same tree here
 * keeps source selection in step with the transcript; scanning the raw string
 * cannot, because only a Markdown parser knows where code and math begin and
 * end.
 *
 * Committed content only: a full Markdown parse costs about 2 ms per KB, so
 * calling this per streamed token would re-parse the whole message each time.
 * Results are cached per content and `citationCount`, because every store
 * update rebuilds the message and citation arrays that callers pass in.
 */
function extractRenderedCitationIndices(
  content: string,
  citationCount: number
): readonly number[] {
  const byCount =
    renderedIndexCache.get(content) ?? new Map<number, readonly number[]>();
  let indices = byCount.get(citationCount);
  if (!indices) {
    indices = parseRenderedCitationIndices(content, citationCount);
    byCount.set(citationCount, indices);
  }
  // Re-insert so Map order tracks recency, then drop the oldest entry.
  renderedIndexCache.delete(content);
  renderedIndexCache.set(content, byCount);
  if (renderedIndexCache.size > RENDERED_INDEX_CACHE_LIMIT) {
    const oldest = renderedIndexCache.keys().next().value;
    if (oldest !== undefined) renderedIndexCache.delete(oldest);
  }
  return indices;
}

function parseRenderedCitationIndices(
  content: string,
  citationCount: number
): number[] {
  const parser = MATH_DELIMITERS.test(content)
    ? mathMarkdownParser
    : plainMarkdownParser;
  const indices = new Set<number>();

  // Mirrors the traversal in CitationRenderer's remark plugin.
  const visit = (node: MarkdownNode, insideLink: boolean): void => {
    for (const child of node.children ?? []) {
      if (child.type === 'text' && !insideLink) {
        for (const segment of parseMessageWithCitations(child.value ?? '', {
          citationCount,
        })) {
          if (segment.citationIndex !== undefined) {
            indices.add(segment.citationIndex);
          }
        }
        continue;
      }
      visit(
        child,
        insideLink || child.type === 'link' || child.type === 'linkReference'
      );
    }
  };
  visit(parser.parse(content) as MarkdownNode, false);

  return Array.from(indices).sort((a, b) => a - b);
}

/**
 * Select the array items referenced by inline citation markers in the content.
 * Only markers the transcript renders as badges count, with the same
 * `citationCount` the renderer uses (see `extractRenderedCitationIndices`).
 *
 * @param content - Assistant message content containing inline citations
 * @param items - Source array aligned to citation numbering (1-based in content)
 * @returns Only the items referenced in the content, in citation order
 */
export function getReferencedItemsByCitationIndex<T>(
  content: string,
  items: T[]
): T[] {
  // Nothing can resolve against an empty list, so skip the parse. Callers
  // such as ChatBubble pass a fresh `[]` on every render.
  if (items.length === 0) return [];

  const indices = extractRenderedCitationIndices(content, items.length);

  if (indices.length === 0) {
    return [];
  }

  return indices
    .map((index) => items[index - 1])
    .filter((item): item is T => item !== undefined);
}

/**
 * Return only the citations referenced inline in the assistant content.
 */
export function getReferencedCitations(
  content: string,
  citations: Citation[]
): Citation[] {
  return getReferencedItemsByCitationIndex(content, citations);
}

/**
 * Match the committed-message source policy: prefer citations that resolve
 * from inline markers, otherwise keep every source attached by the backend.
 * Invalid-only markers therefore follow the same fallback as no markers.
 */
export function getVisibleCitations(
  content: string,
  citations: Citation[]
): Citation[] {
  const referenced = getReferencedCitations(content, citations);
  return referenced.length > 0 ? referenced : citations;
}

/**
 * Get citation by index from citations array
 *
 * @param citations - Array of citations from message
 * @param index - 1-based citation index
 * @returns The citation at the given index, or undefined
 */
export function getCitationByIndex(
  citations: Citation[],
  index: number
): Citation | undefined {
  // Citations array is 0-indexed, but [Doc N] uses 1-based indexing
  return citations[index - 1];
}

/**
 * Format citation for display in tooltip
 *
 * @param citation - The citation to format
 * @returns Formatted citation info
 */
export function formatCitationPreview(citation: Citation): string {
  const score = Math.round(citation.score * 100);
  return `${citation.title} (${score}% relevance)`;
}

/**
 * Calculate relevance badge color based on score
 *
 * @param score - Score from 0-1
 * @returns CSS color class
 */
export function getScoreColor(score: number): string {
  if (score >= 0.8) return 'text-green-500';
  if (score >= 0.6) return 'text-yellow-500';
  if (score >= 0.4) return 'text-orange-500';
  return 'text-red-500';
}

/**
 * Truncate text to specified length with ellipsis
 *
 * @param text - Text to truncate
 * @param maxLength - Maximum length
 * @returns Truncated text
 */
export function truncateText(text: string, maxLength: number = 150): string {
  if (text.length <= maxLength) return text;
  return text.slice(0, maxLength).trim() + '...';
}
