/**
 * GOO-722: an image document with no thumbnail must not fall back to a
 * third-party placeholder host (it sent the file name off-site, and CSP
 * img-src now blocks remote https images anyway).
 */
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { MultimodalViewer } from '../MultimodalViewer';

const base = {
  id: 'doc-1',
  title: 'Scan',
  filename: 'private-scan.png',
  file_type: 'png' as const,
  file_size: 10,
  processing_status: 'completed',
};

describe('MultimodalViewer image fallback', () => {
  it('renders no remote image when there is no thumbnail', async () => {
    const { container } = render(<MultimodalViewer document={base} />);
    expect(await screen.findByText('No preview available')).toBeInTheDocument();
    expect(container.querySelector('img')).toBeNull();
    expect(container.innerHTML).not.toContain('private-scan.png?');
    expect(container.innerHTML).not.toContain('placeholder.com');
  });

  it('renders the thumbnail when one is provided', async () => {
    render(
      <MultimodalViewer
        document={{ ...base, thumbnail_url: 'blob:https://app/thumb' }}
      />
    );
    expect(await screen.findByAltText('Scan')).toHaveAttribute(
      'src',
      'blob:https://app/thumb'
    );
  });
});
