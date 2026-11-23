import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { Mock } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import toast from 'react-hot-toast';
import { render } from '@/test/test-utils';
import { GraphHealthMonitor } from '@/components/entities/GraphHealthMonitor';
import { api } from '@/services/api-client';

vi.mock('react-hot-toast', () => ({
  __esModule: true,
  default: {
    success: vi.fn(),
    error: vi.fn(),
  },
}));

vi.mock('@/services/api-client', () => ({
  api: {
    get: vi.fn(),
  },
}));

describe('GraphHealthMonitor', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders health metrics when API returns backend health shape', async () => {
    (api.get as Mock).mockResolvedValue({
      status: 'healthy',
      neo4j_version: '5.0',
      database_size: '1.2 GB',
      node_count: 1234,
      relationship_count: 5678,
      index_count: 4,
      constraint_count: 2,
      uptime: '1d 2h',
      last_error: null,
      response_time_ms: 10,
    });

    render(<GraphHealthMonitor />);

    expect(await screen.findByText('1,234')).toBeInTheDocument();
    expect(screen.getByText('5,678')).toBeInTheDocument();
  });

  it('requests the leading-slash health path and toasts on failure', async () => {
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {});
    (api.get as Mock).mockRejectedValue(new Error('down'));

    render(<GraphHealthMonitor />);

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith('Failed to fetch graph health')
    );
    expect(api.get).toHaveBeenCalledWith('/knowledge-graph/health');
    consoleError.mockRestore();
  });

  it('shows a degraded graph as degraded and invents no health score (FR-3)', async () => {
    (api.get as Mock).mockResolvedValue({
      status: 'degraded',
      neo4j_version: '5.0',
      node_count: 10,
      relationship_count: 3,
      index_count: 0,
      constraint_count: 0,
      last_error: 'slow query',
      response_time_ms: 900,
    });

    render(<GraphHealthMonitor />);

    expect(await screen.findByText('degraded')).toBeInTheDocument();
    expect(screen.queryByText(/health score/i)).not.toBeInTheDocument();
    expect(screen.queryByText('/100')).not.toBeInTheDocument();
    expect(screen.queryByText(/last check:/i)).not.toBeInTheDocument();
    expect(screen.getByText('Last error: slow query')).toBeInTheDocument();
  });

  it('shows an unrecognised status as unknown, never healthy (FR-3)', async () => {
    (api.get as Mock).mockResolvedValue({
      status: 'starting',
      neo4j_version: '5.0',
      node_count: 0,
      relationship_count: 0,
      index_count: 0,
      constraint_count: 0,
      response_time_ms: 1,
    });

    render(<GraphHealthMonitor />);

    expect(await screen.findByText('unknown')).toBeInTheDocument();
    expect(screen.queryByText('healthy')).not.toBeInTheDocument();
  });

  it('renders an explicit unavailable state when the request fails (FR-3)', async () => {
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {});
    (api.get as Mock).mockRejectedValue(new Error('down'));

    render(<GraphHealthMonitor />);

    expect(await screen.findByRole('alert')).toHaveTextContent('Graph health is unavailable');
    consoleError.mockRestore();
  });
});
