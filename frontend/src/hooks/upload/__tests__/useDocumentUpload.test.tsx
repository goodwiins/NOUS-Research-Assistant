/**
 * GOO-723: the upload queue is a page-lifetime singleton. Finished items used
 * to stay in it and count toward maxFiles, so after maxFiles uploads in one
 * tab every new batch was rejected with TOO_MANY_FILES until a reload.
 */
import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('@/services/api-client', () => ({
  api: {
    upload: vi.fn(),
    get: vi.fn(),
  },
}));

import { api } from '@/services/api-client';
import { uploadService } from '@/services/uploadService';
import { useDocumentUpload } from '@/hooks/upload/useDocumentUpload';

const uploadMock = vi.mocked(api.upload);
const getMock = vi.mocked(api.get);

function pdf(name: string): File {
  return new File(['%PDF-1.4'], name, { type: 'application/pdf' });
}

async function settle(): Promise<void> {
  // Upload resolves, then the first status poll reports completion.
  await act(async () => {
    await vi.advanceTimersByTimeAsync(3000);
  });
}

describe('useDocumentUpload maxFiles across batches (GOO-723)', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    uploadMock.mockReset();
    getMock.mockReset();
    uploadService.cancelAllUploads();
    let n = 0;
    uploadMock.mockImplementation(async () => {
      n += 1;
      return { document_id: `doc-${n}` } as never;
    });
    getMock.mockResolvedValue({
      processing_status: 'completed',
      is_embedded: true,
      is_indexed: true,
      processing_error: null,
    });
  });

  afterEach(() => {
    uploadService.cancelAllUploads();
    vi.useRealTimers();
  });

  it('accepts more than maxFiles files over several batches in one session', async () => {
    // Batches of 4 under maxFiles 5: counting the previous, finished batch
    // would reject every batch after the first (4 + 4 > 5).
    const { result } = renderHook(() =>
      useDocumentUpload({ maxFiles: 5, autoCleanup: false })
    );

    for (let batch = 0; batch < 3; batch += 1) {
      const files = [0, 1, 2, 3].map((i) => pdf(`b${batch}-${i}.pdf`));
      let outcome: ReturnType<typeof result.current.addToQueue> | undefined;
      act(() => {
        outcome = result.current.addToQueue(files);
      });
      expect(outcome?.errors).toEqual([]);
      expect(outcome?.success).toHaveLength(4);
      await settle();
      expect(
        uploadService.getQueueItems().filter((i) => i.status === 'completed')
      ).toHaveLength(4);
    }

    expect(uploadMock).toHaveBeenCalledTimes(12);
  });

  it('still counts unfinished uploads toward maxFiles', () => {
    // Nothing settles: the first batch stays pending/uploading.
    uploadMock.mockImplementation(() => new Promise(() => {}));
    const { result } = renderHook(() =>
      useDocumentUpload({ maxFiles: 5, autoCleanup: false })
    );

    act(() => {
      result.current.addToQueue([0, 1, 2, 3].map((i) => pdf(`a${i}.pdf`)));
    });
    let outcome: ReturnType<typeof result.current.addToQueue> | undefined;
    act(() => {
      outcome = result.current.addToQueue([pdf('x.pdf'), pdf('y.pdf')]);
    });

    expect(outcome?.success).toEqual([]);
    expect(outcome?.errors.map((e) => e.code)).toContain('TOO_MANY_FILES');
  });

  it('leaves the queue empty after cancelAllUploads()', async () => {
    const { result } = renderHook(() =>
      useDocumentUpload({ maxFiles: 10, autoCleanup: false })
    );
    act(() => {
      result.current.addToQueue([pdf('one.pdf'), pdf('two.pdf')]);
    });
    await settle();

    act(() => {
      result.current.cancelAllUploads();
    });

    expect(uploadService.getQueueItems()).toEqual([]);
    expect(result.current.queueItems).toEqual([]);
  });
});
