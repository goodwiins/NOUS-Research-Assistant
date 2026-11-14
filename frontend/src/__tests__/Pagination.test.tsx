import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { Pagination } from '../../app/components/Pagination';

describe('Pagination', () => {
  it('gives the icon-only navigation buttons accessible names', () => {
    const onPageChange = vi.fn();
    render(
      <Pagination
        currentPage={2}
        totalPages={5}
        pageSize={10}
        totalItems={50}
        onPageChange={onPageChange}
      />
    );
    for (const name of ['First Page', 'Previous Page', 'Next Page', 'Last Page']) {
      expect(screen.getByRole('button', { name })).toBeInTheDocument();
    }
    fireEvent.click(screen.getByRole('button', { name: 'Next Page' }));
    expect(onPageChange).toHaveBeenCalledWith(3);
  });
});
