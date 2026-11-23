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
/** The backend's default page size (`size: Query(20)`). */
export const DEFAULT_DOCUMENT_PAGE_SIZE = 20;

type FrontendFileType = NonNullable<DocumentFilters['file_type']>;

/** A file type with no backend DocumentType: a programming error. */
export class UnmappedDocumentTypeError extends Error {
  constructor(readonly fileType: string) {
    super(`No backend document_type for file type "${fileType}"`);
    this.name = 'UnmappedDocumentTypeError';
  }
}

// Frontend file types (extensions and modalities) -> backend DocumentType.
// Exhaustive by type: a new frontend file type fails type-check until it is
// mapped here.
const FILE_TYPE_TO_DOCUMENT_TYPE: Readonly<Record<FrontendFileType, BackendDocumentType>> = {
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

const isProduction = (): boolean => process.env.NODE_ENV === 'production';

function toDocumentType(fileType: string): BackendDocumentType | undefined {
  const mapped = (FILE_TYPE_TO_DOCUMENT_TYPE as Readonly<Record<string, BackendDocumentType>>)[fileType];
  if (mapped) return mapped;
  // Only reachable through a cast. Fail loudly where a developer will see
  // it; in production send no type filter rather than a value the backend
  // rejects with a 422.
  if (!isProduction()) throw new UnmappedDocumentTypeError(fileType);
  return undefined;
}

/** A positive integer, or the fallback for NaN, Infinity and non-numbers. */
const positiveInt = (value: number, fallback: number): number =>
  Number.isFinite(value) ? Math.max(1, Math.floor(value)) : fallback;

/** Build the list query: one value per filter, by type. */
export function buildDocumentListQuery(
  page: number,
  pageSize: number,
  filters?: DocumentFilters
): DocumentListQuery {
  const query: DocumentListQuery = {
    page: positiveInt(page, 1),
    size: Math.min(
      MAX_DOCUMENT_PAGE_SIZE,
      positiveInt(pageSize, DEFAULT_DOCUMENT_PAGE_SIZE)
    ),
  };
  if (filters?.file_type) {
    const documentType = toDocumentType(filters.file_type);
    if (documentType) query.document_type = documentType;
  }
  if (filters?.status) query.processing_status = filters.status;
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
