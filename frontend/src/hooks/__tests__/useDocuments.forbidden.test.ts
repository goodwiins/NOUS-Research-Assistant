/**
 * GOO-651 (FL-2): a 403 means "you may not do this", not "your session is
 * gone". Only a 401 may sign the user out. A forbidden delete, reprocess or
 * list must surface a permission error and leave the session alone, and a
 * forbidden id must not abort the rest of a bulk delete.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { renderHook, act, waitFor } from '@testing-library/react';
import { useDocuments } from '../useDocuments';
import { APIErrorClass } from '@/types';

const mockGet = vi.fn();
const mockDelete = vi.fn();
const mockPost = vi.fn();

vi.mock('@/services/api-client', () => ({
  api: {
    get: (...args: unknown[]) => mockGet(...args),
    delete: (...args: unknown[]) => mockDelete(...args),
    post: (...args: unknown[]) => mockPost(...args),
  },
}));

const mockHandleAuthError = vi.fn();

vi.mock('@/hooks/useAuth', () => ({
  useAuth: () => ({
    isAuthenticated: true,
    isLoading: false,
    handleAuthError: mockHandleAuthError,
  }),
}));

const forbidden = (): APIErrorClass =>
  new APIErrorClass({
    message: 'Insufficient permissions',
    status_code: 403,
    type: 'forbidden',
  });

const unauthorized = (): APIErrorClass =>
  new APIErrorClass({
    message: 'Session expired',
    status_code: 401,
    type: 'auth_error',
  });

const EMPTY_PAGE = {
  documents: [],
  pagination: {
    page: 1,
    page_size: 20,
    total: 0,
    total_pages: 0,
    has_next: false,
    has_prev: false,
  },
};

beforeEach(() => {
  vi.clearAllMocks();
  vi.spyOn(console, 'error').mockImplementation(() => undefined);
  mockGet.mockResolvedValue(EMPTY_PAGE);
  mockDelete.mockResolvedValue(undefined);
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('useDocuments 403 handling', () => {
  it('a forbidden single delete reports a permission error and keeps the session', async () => {
    mockDelete.mockRejectedValue(forbidden());
    const { result } = renderHook(() => useDocuments({ autoFetch: false }));

    let caught: unknown;
    await act(async () => {
      try {
        await result.current.deleteDocument('a');
      } catch (error) {
        caught = error;
      }
    });

    expect(mockHandleAuthError).not.toHaveBeenCalled();
    expect((caught as Error).message).toMatch(/permission/i);
    expect((caught as Error).message).not.toMatch(/session/i);
  });

  it('a forbidden id does not abort the rest of a bulk delete', async () => {
    mockDelete.mockImplementation((path: string) =>
      path === '/documents/b'
        ? Promise.reject(forbidden())
        : Promise.resolve(undefined)
    );
    const { result } = renderHook(() => useDocuments({ autoFetch: false }));

    let caught: unknown;
    await act(async () => {
      try {
        await result.current.deleteDocuments(['a', 'b', 'c']);
      } catch (error) {
        caught = error;
      }
    });

    expect(mockDelete).toHaveBeenCalledTimes(3);
    expect(mockDelete).toHaveBeenCalledWith('/documents/c');
    expect(mockHandleAuthError).not.toHaveBeenCalled();
    expect((caught as Error).message).toMatch(/b: .*permission/i);
    expect((caught as Error).message).not.toMatch(/skipped/);
  });

  it('a forbidden reprocess reports a permission error and keeps the session', async () => {
    mockPost.mockRejectedValue(forbidden());
    const { result } = renderHook(() => useDocuments({ autoFetch: false }));

    let caught: unknown;
    await act(async () => {
      try {
        await result.current.retryDocument('a');
      } catch (error) {
        caught = error;
      }
    });

    expect(mockHandleAuthError).not.toHaveBeenCalled();
    expect((caught as Error).message).toMatch(/permission/i);
    expect(result.current.error).toMatch(/permission/i);
  });

  it('a forbidden list fetch reports a permission error and keeps the session', async () => {
    mockGet.mockRejectedValue(forbidden());
    const { result } = renderHook(() => useDocuments({ autoFetch: false }));

    await act(async () => {
      await result.current.fetchDocuments();
    });

    await waitFor(() => expect(result.current.error).toMatch(/permission/i));
    expect(mockHandleAuthError).not.toHaveBeenCalled();
  });

  it('a 401 still signs the user out', async () => {
    mockGet.mockRejectedValue(unauthorized());
    const { result } = renderHook(() => useDocuments({ autoFetch: false }));

    await act(async () => {
      await result.current.fetchDocuments();
    });

    expect(mockHandleAuthError).toHaveBeenCalledTimes(1);
  });
});
