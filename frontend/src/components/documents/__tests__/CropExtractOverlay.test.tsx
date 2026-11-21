import { createRef } from 'react';
import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@/test/test-utils';
import { CropExtractOverlay } from '../CropExtractOverlay';
import { extractRegion } from '@/services/scispaceService';

vi.mock('@/services/scispaceService', () => ({
  extractRegion: vi.fn(),
}));

function drawSelection(container: HTMLElement): void {
  const overlay = screen.getByText('Click and drag to select a region')
    .closest('.cursor-crosshair') as HTMLElement;
  vi.spyOn(container, 'getBoundingClientRect').mockReturnValue({
    left: 0,
    top: 0,
    width: 500,
    height: 500,
  } as DOMRect);
  fireEvent.mouseDown(overlay, { clientX: 10, clientY: 10 });
  fireEvent.mouseMove(overlay, { clientX: 110, clientY: 90 });
  fireEvent.mouseUp(overlay, { clientX: 110, clientY: 90 });
}

describe('CropExtractOverlay (FR-8)', () => {
  it('shows an error when region extraction fails instead of swallowing it', async () => {
    vi.mocked(extractRegion).mockRejectedValue(new Error('boom'));
    const container = document.createElement('div');
    const ref = createRef<HTMLElement>() as { current: HTMLElement | null };
    ref.current = container;
    const onExtracted = vi.fn();

    render(
      <CropExtractOverlay
        active
        documentId="doc-1"
        pageNumber={1}
        containerRef={ref}
        onExtracted={onExtracted}
        onCancel={vi.fn()}
      />
    );
    drawSelection(container);
    fireEvent.click(
      screen.getByRole('button', { name: 'Extract selected region' })
    );

    expect(await screen.findByRole('alert')).toHaveTextContent(
      /Region extraction failed/
    );
    expect(onExtracted).not.toHaveBeenCalled();
    expect(
      screen.getByRole('button', { name: 'Extract selected region' })
    ).toBeEnabled();
  });
});
