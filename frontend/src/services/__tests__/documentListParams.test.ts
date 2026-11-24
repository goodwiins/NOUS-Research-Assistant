/**
 * GOO-666 FL2C-3: the documents list must use the backend's own query names
 * (size, document_type, processing_status) and the shapes it accepts.
 * Names, allowed values and the response fixture come from the committed
 * backend/openapi.json, not from a hand-written copy.
 */
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { act, renderHook, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

const mockGet = vi.fn();
vi.mock('@/services/api-client', () => ({
  api: {
    get: (...args: unknown[]) => mockGet(...args),
    delete: vi.fn(),
    post: vi.fn(),
  },
}));
// One stable object: a new handleAuthError per render would re-create
// fetchDocuments and re-run the auto-fetch effect forever.
const mockAuth = { isAuthenticated: true, isLoading: false, handleAuthError: vi.fn() };
vi.mock('@/hooks/useAuth', () => ({
  useAuth: () => mockAuth,
}));

import {
  DEFAULT_DOCUMENT_PAGE_SIZE,
  MAX_DOCUMENT_PAGE_SIZE,
  UnmappedDocumentTypeError,
  buildDocumentListQuery,
  documentListQueryString,
} from '../documentListParams';
import { documentService } from '../documentService';
import { useDocuments } from '@/hooks/useDocuments';

interface OpenApiParam {
  name: string;
  in: string;
  schema: { maximum?: number; default?: number; anyOf?: Array<{ $ref?: string }> };
}
const openapi = JSON.parse(
  readFileSync(resolve(__dirname, '../../../../backend/openapi.json'), 'utf8')
) as {
  paths: Record<string, { get: { parameters: OpenApiParam[]; responses: Record<string, { content: { 'application/json': { example: unknown } } }> } }>;
  components: { schemas: { DocumentType: { enum: string[] } } };
};
const listOp = openapi.paths['/api/v1/documents'].get;
const backendParams = new Map(listOp.parameters.map((p) => [p.name, p]));
const realResponse = listOp.responses['200'].content['application/json'].example as {
  documents: unknown[];
  pagination: Record<string, unknown>;
};
const documentTypes = openapi.components.schemas.DocumentType.enum;

const query = (url: string): URLSearchParams =>
  new URL(url, 'http://localhost').searchParams;

afterEach(() => {
  mockGet.mockReset();
});

describe('buildDocumentListQuery', () => {
  it('only sends query names the backend route declares', () => {
    const built = buildDocumentListQuery(2, 10, {
      file_type: 'pdf',
      status: 'indexed',
      search_term: 'graphs',
      date_range: { start: '2026-01-01T00:00:00Z', end: '2026-02-01T00:00:00Z' },
    });
    for (const name of Object.keys(built)) {
      expect(backendParams.get(name)?.in, name).toBe('query');
    }
    expect(built).toEqual({
      page: 2,
      size: 10,
      document_type: 'pdf',
      processing_status: 'indexed',
      search: 'graphs',
      date_from: '2026-01-01T00:00:00Z',
      date_to: '2026-02-01T00:00:00Z',
    });
  });

  it('maps every frontend file type to a backend DocumentType value', () => {
    for (const fileType of [
      'pdf', 'txt', 'jpg', 'png', 'mp3', 'mp4', 'text', 'image', 'audio',
      'video', 'spreadsheet', 'presentation', 'multimodal',
    ] as const) {
      const built = buildDocumentListQuery(1, 20, { file_type: fileType });
      expect(documentTypes, fileType).toContain(built.document_type);
    }
  });

  it('throws a named error for an unmapped file type outside production', () => {
    const cast = { file_type: 'docx' } as unknown as Parameters<typeof buildDocumentListQuery>[2];
    expect(() => buildDocumentListQuery(1, 20, cast)).toThrow(UnmappedDocumentTypeError);
    expect(() => buildDocumentListQuery(1, 20, cast)).toThrow(/"docx"/);
  });

  it('in production sends no type filter for an unmapped file type', () => {
    vi.stubEnv('NODE_ENV', 'production');
    try {
      const cast = { file_type: 'docx' } as unknown as Parameters<typeof buildDocumentListQuery>[2];
      expect(buildDocumentListQuery(1, 20, cast)).toEqual({ page: 1, size: 20 });
    } finally {
      vi.unstubAllEnvs();
    }
  });

  it('clamps the page size to the backend bound', () => {
    expect(backendParams.get('size')?.schema.maximum).toBe(MAX_DOCUMENT_PAGE_SIZE);
    expect(buildDocumentListQuery(1, 500).size).toBe(MAX_DOCUMENT_PAGE_SIZE);
    expect(buildDocumentListQuery(0, 0)).toEqual({ page: 1, size: 1 });
  });

  it('falls back to the defaults for NaN and infinite page numbers', () => {
    for (const bad of [Number.NaN, Number.POSITIVE_INFINITY, Number.NEGATIVE_INFINITY]) {
      expect(buildDocumentListQuery(bad, bad)).toEqual({
        page: 1,
        size: DEFAULT_DOCUMENT_PAGE_SIZE,
      });
    }
    expect(backendParams.get('size')?.schema).toMatchObject({ default: DEFAULT_DOCUMENT_PAGE_SIZE });
  });

  it('serialises without page_size, file_types or status', () => {
    const q = query(`?${documentListQueryString(1, 10, { status: 'queued' })}`);
    expect(q.get('size')).toBe('10');
    expect(q.get('processing_status')).toBe('queued');
    for (const stale of ['page_size', 'file_types', 'status']) {
      expect(q.has(stale), stale).toBe(false);
    }
  });
});

describe('callers', () => {
  it('documentService.getDocuments sends the backend names', async () => {
    mockGet.mockResolvedValue(realResponse);
    await documentService.getDocuments(1, 10, { file_type: 'mp4', status: 'failed' });
    const q = query(mockGet.mock.calls[0][0] as string);
    expect(Object.fromEntries(q)).toEqual({
      page: '1',
      size: '10',
      document_type: 'video',
      processing_status: 'failed',
    });
  });

  it('useDocuments requests its page size and status filter, and reads the real response', async () => {
    // Like the route, echo the requested size in pagination.page_size.
    mockGet.mockImplementation(async (url: string) => ({
      ...realResponse,
      pagination: { ...realResponse.pagination, page_size: Number(query(url).get('size')) },
    }));
    const { result } = renderHook(() =>
      useDocuments({ autoFetch: true, initialPageSize: 10 })
    );
    await waitFor(() =>
      expect(result.current.documents).toHaveLength(realResponse.documents.length)
    );
    expect(realResponse.documents.length).toBeGreaterThan(0);
    expect(query(mockGet.mock.calls[0][0] as string).get('size')).toBe('10');
    expect(result.current.pagination.total).toBe(realResponse.pagination.total);

    await act(async () => {
      result.current.updateFilters({ status: 'indexed' });
    });
    const last = query(mockGet.mock.calls.at(-1)?.[0] as string);
    expect(last.get('processing_status')).toBe('indexed');
    expect(last.get('size')).toBe('10');
  });
});
