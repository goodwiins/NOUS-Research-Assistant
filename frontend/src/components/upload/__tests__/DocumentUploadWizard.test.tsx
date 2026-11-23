import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { FileValidationError } from '@/utils/fileValidation';
import { DocumentUploadWizard } from '../DocumentUploadWizard';

const addToQueue = vi.fn<(files: File[]) => { success: string[]; errors: FileValidationError[] }>();

vi.mock('@/hooks/upload/useDocumentUpload', () => ({
  useDocumentUpload: () => ({
    addToQueue,
    removeFromQueue: vi.fn(),
    retryUpload: vi.fn(),
    cancelAllUploads: vi.fn(),
    queueItems: [],
    stats: { totalFiles: 0, completedFiles: 0, failedFiles: 0, totalSize: 0, uploadedSize: 0 },
    isUploading: false,
    hasActiveUploads: false,
    allCompleted: false,
    hasErrors: false,
    overallProgress: 0,
    uploadSpeed: 0,
    estimatedTimeRemaining: 0,
    formatFileSize: (bytes: number) => `${bytes} B`,
    formatTime: (s: number) => `${s}s`,
    getStatusText: (s: string) => s,
    getFileIcon: () => 'icon',
  }),
}));

function renderWizard(): ReturnType<typeof render> {
  return render(<DocumentUploadWizard isOpen onClose={vi.fn()} />);
}

async function dropFile(file: File): Promise<void> {
  const input = document.querySelector('input[type="file"]') as HTMLInputElement;
  fireEvent.change(input, { target: { files: [file] } });
  await screen.findByText(file.name);
}

describe('DocumentUploadWizard', () => {
  beforeEach(() => {
    addToQueue.mockReset();
  });

  it('keeps the dropped File intact so name, size and type render (FR-1)', async () => {
    renderWizard();
    const pdf = new File(['%PDF-1.4 hello'], 'paper.pdf', { type: 'application/pdf' });

    await dropFile(pdf);

    expect(screen.getByText('paper.pdf')).toBeInTheDocument();
    expect(screen.getByText(/application\/pdf/)).toBeInTheDocument();
  });

  it('passes the original File objects to the upload queue (FR-1)', async () => {
    addToQueue.mockReturnValue({ success: ['q1'], errors: [] });
    renderWizard();
    const pdf = new File(['%PDF-1.4 hello'], 'paper.pdf', { type: 'application/pdf' });
    await dropFile(pdf);

    fireEvent.click(screen.getByRole('button', { name: /next/i }));
    await screen.findByText('Review Files');
    fireEvent.click(screen.getByRole('button', { name: /next/i }));

    await screen.findByText('Processing Files');
    const [queued] = addToQueue.mock.calls[0];
    expect(queued[0]).toBe(pdf);
    expect(queued[0]).toBeInstanceOf(File);
  });

  it('stays on review and shows the errors when the queue rejects the batch (FR-2)', async () => {
    addToQueue.mockReturnValue({
      success: [],
      errors: [{ code: 'QUOTA_EXCEEDED', message: 'quota' }],
    });
    renderWizard();
    await dropFile(new File(['x'], 'paper.pdf', { type: 'application/pdf' }));

    fireEvent.click(screen.getByRole('button', { name: /next/i }));
    await screen.findByText('Review Files');
    fireEvent.click(screen.getByRole('button', { name: /next/i }));

    await waitFor(() => expect(addToQueue).toHaveBeenCalled());
    expect(screen.getByText('Review Files')).toBeInTheDocument();
    expect(screen.queryByText('Processing Files')).not.toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent(/Upload Validation Errors/);
  });
});
