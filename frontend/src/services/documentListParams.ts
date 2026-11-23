/**
 * Query string for GET /api/v1/documents (GOO-666 FL2C-3).
 *
 * Typed against the generated operation, so a renamed or removed backend
 * parameter fails type-check here instead of being silently ignored. The
 * backend reads `size` (not `page_size`) and ONE `document_type` and ONE
 * `processing_status`; a comma-joined list is rejected (422 / 400).
 */
import type { components, operations } from '@/types/generated/api';
import type { DocumentFilters } from '@/types/document';

export type DocumentListQuery = NonNullable<
  operations['list_documents_api_v1_documents_get']['parameters']['query']
>;
type BackendDocumentType = components['schemas']['DocumentType'];

/** The backend's page-size bound (`size: Query(ge=1, le=100)`). */
export const MAX_DOCUMENT_PAGE_SIZE = 100;

// Frontend file types (extensions and modalities) -> backend DocumentType.
const FILE_TYPE_TO_DOCUMENT_TYPE: Readonly<Record<string, BackendDocumentType>> = {
  pdf: 'pdf',
  txt: 'text',
  text: 'text',
  jpg: 'image',
  png: 'image',
  image: 'image',
  mp3: 'audio',
  audio: 'audio',
  mp4: 'video',
  video: 'video',
  spreadsheet: 'spreadsheet',
  presentation: 'presentation',
  multimodal: 'multimodal',
};

/** One value, or undefined when the selection is empty or names several. */
function single<T>(values: Iterable<T>): T | undefined {
  const unique = new Set(values);
  return unique.size === 1 ? unique.values().next().value : undefined;
}

/**
 * Build the list query. A filter that resolves to several values cannot be
 * expressed (the backend takes one), so it is not sent rather than sent in a
 * form the backend rejects.
 */
export function buildDocumentListQuery(
  page: number,
  pageSize: number,
  filters?: DocumentFilters
): DocumentListQuery {
  const query: DocumentListQuery = {
    page: Math.max(1, Math.floor(page)),
    size: Math.min(MAX_DOCUMENT_PAGE_SIZE, Math.max(1, Math.floor(pageSize))),
  };
  const documentType = single(
    (filters?.file_types ?? []).flatMap((type) => {
      const mapped = type ? FILE_TYPE_TO_DOCUMENT_TYPE[type] : undefined;
      return mapped ? [mapped] : [];
    })
  );
  if (documentType) query.document_type = documentType;
  const status = single(filters?.status ?? []);
  if (status) query.processing_status = status;
  if (filters?.search_term) query.search = filters.search_term;
  if (filters?.date_range) {
    query.date_from = filters.date_range.start;
    query.date_to = filters.date_range.end;
  }
  return query;
}

export function documentListQueryString(
  page: number,
  pageSize: number,
  filters?: DocumentFilters
): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(
    buildDocumentListQuery(page, pageSize, filters)
  )) {
    if (value !== undefined && value !== null) params.set(key, String(value));
  }
  return params.toString();
}
