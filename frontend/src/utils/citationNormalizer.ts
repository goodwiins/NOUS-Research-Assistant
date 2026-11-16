import type { Citation as DBCitation } from '@/types/workspace';
import type { Citation } from '@/utils/citationParser';

export const MAX_CITATION_TITLE_INPUT_LENGTH = 4096;

export function extractStructuredCitationTitle(snippet: string): string | null {
  const boundedSnippet = snippet.slice(0, MAX_CITATION_TITLE_INPUT_LENGTH);
  if (!/^Title:/i.test(boundedSnippet)) return null;

  const text = boundedSnippet.slice(6).trimStart();
  const lineEnd = text.search(/[\r\n]/);
  const firstLine = lineEnd < 0 ? text : text.slice(0, lineEnd);
  const authorsAt = firstLine.search(/\sAuthors:/i);
  const title = (
    authorsAt < 0 ? firstLine : firstLine.slice(0, authorsAt)
  ).trim();
  return title || null;
}

/**
 * Extract title from snippet content (for legacy citations without document_title)
 * Handles formats like "Title: Some Title Authors: ..." or plain text
 */
export function extractTitleFromSnippet(snippet?: string): string | null {
  if (!snippet) return null;

  const boundedSnippet = snippet.slice(0, MAX_CITATION_TITLE_INPUT_LENGTH);

  // Try to extract "Title: <title>" pattern (common in arXiv papers)
  const structuredTitle = extractStructuredCitationTitle(boundedSnippet);
  if (structuredTitle) return structuredTitle;

  // Fallback: use first line IF it looks like a title (not code, URLs, or log lines)
  const firstLine = boundedSnippet.split('\n')[0].trim();
  if (firstLine.length > 0 && firstLine.length <= 120) {
    // Skip lines that look like code, URLs, logs, or raw data
    const looksLikeNonTitle =
      /^[{(\[<`]/.test(firstLine) || // Starts with code brackets
      /[{};=>\[\]`]/.test(firstLine) || // Contains code syntax
      /https?:\/\//.test(firstLine) || // Contains URLs
      /\d{4}-\d{2}-\d{2}/.test(firstLine) || // Contains timestamps
      /duration_ms|count=|debug|error|warn/i.test(firstLine) || // Log lines
      /^\w+=\d/.test(firstLine); // Key=value patterns

    if (!looksLikeNonTitle) {
      return firstLine;
    }
  }

  // Don't use garbled content as title - return null to trigger "Unknown Document" fallback
  return null;
}

/**
 * Normalize a citation from any format (database snake_case or API camelCase)
 * to the citationParser format expected by CitationRenderer and CitationLink.
 *
 * This handles the mismatch between:
 * - Database format: { document_id, external_reference_id, document_title, snippet, score }
 * - Parser format: { documentId, externalReferenceId, title, score, content, source }
 */
export function normalizeCitation(
  citation: DBCitation | Citation | Record<string, unknown>
): Citation {
  const c = citation as Record<string, unknown>;
  // Handle both snake_case (from DB) and camelCase (from API response)
  const documentId = (c.documentId as string) || (c.document_id as string);
  const externalReferenceId =
    (c.externalReferenceId as string) || (c.external_reference_id as string);
  const snippet =
    (c.content as string) ||
    (c.snippet as string) ||
    (c.snippet_preview as string);

  return {
    // Only set documentId if it's a valid non-empty value
    documentId: documentId || undefined,
    // Support external references (e.g., arXiv paper IDs)
    externalReferenceId: externalReferenceId || undefined,
    // Extract title from snippet if document_title is missing
    title:
      (c.title as string) ||
      (c.document_title as string) ||
      extractTitleFromSnippet(snippet) ||
      'Unknown Document',
    score: (c.score as number) ?? 0,
    content: snippet,
    source: (c.source as string) || (c.document_type as string),
    sourcePosition:
      (c.sourcePosition as number) ?? (c.source_position as number),
    chunkId: (c.chunkId as string) || (c.chunk_id as string) || undefined,
    chunkIndex: (c.chunkIndex as number) ?? (c.chunk_index as number),
    pageNumber: (c.pageNumber as number) ?? (c.page_number as number),
  };
}
