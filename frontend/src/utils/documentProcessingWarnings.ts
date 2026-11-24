/**
 * "Processed with warnings" (GOO-744). Since #2272 (GOO-658) a document whose
 * optional processing steps failed still completes, and the backend records
 * that in its metadata: `processing_partial: true` plus `processing_warnings`,
 * one stable message per failed step. Only the detail endpoint
 * (`DocumentDetailResponse.metadata`) carries them; the list response has no
 * metadata, so a list row cannot tell a partial document from a clean one.
 */
export interface ProcessingWarnings {
  /** The document completed, but without some optional output. */
  partial: boolean;
  /** Per-step messages; empty when the document is not partial. */
  warnings: string[];
}

const CLEAN: ProcessingWarnings = { partial: false, warnings: [] };

export function getProcessingWarnings(
  metadata: Record<string, unknown> | null | undefined
): ProcessingWarnings {
  if (!metadata || metadata.processing_partial !== true) return CLEAN;
  const raw = metadata.processing_warnings;
  const warnings = Array.isArray(raw)
    ? raw.filter((w): w is string => typeof w === 'string' && w.trim() !== '')
    : [];
  // The backend sets partial only together with warnings. A partial flag with
  // nothing readable is still not a clean run, so it stays partial.
  return { partial: true, warnings };
}
