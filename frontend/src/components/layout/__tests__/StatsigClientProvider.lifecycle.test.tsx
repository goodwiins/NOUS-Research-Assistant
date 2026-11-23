import { render, screen } from '@testing-library/react';
import { renderToString } from 'react-dom/server';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { ReactNode } from 'react';

const mockInitializeAsync = vi.fn().mockResolvedValue(undefined);
const mockUpdateUserAsync = vi.fn().mockResolvedValue(undefined);
const mockUseClientAsyncInit = vi.fn();

const mockClient = {
  initializeAsync: mockInitializeAsync,
  updateUserAsync: mockUpdateUserAsync,
  loadingStatus: 'Ready',
  $on: vi.fn(),
  off: vi.fn(),
  flush: vi.fn().mockResolvedValue(undefined),
};

vi.mock('@/hooks/useAuth', () => ({
  useAuth: () => ({ user: null }),
}));

vi.mock('@statsig/web-analytics', () => ({
  StatsigAutoCapturePlugin: class StatsigAutoCapturePlugin {},
}));

vi.mock('@statsig/react-bindings', () => ({
  LogLevel: { Warn: 'Warn', Debug: 'Debug' },
  StatsigClient: vi.fn(() => mockClient),
  StatsigProvider: ({ children }: { children: ReactNode }) => (
    <div data-testid="statsig-provider">{children}</div>
  ),
  useClientAsyncInit: (...args: unknown[]) => {
    mockUseClientAsyncInit(...args);
    return { client: mockClient, isLoading: false };
  },
}));

describe('StatsigClientProvider lifecycle', () => {
  beforeEach(() => {
    vi.resetModules();
    process.env.NEXT_PUBLIC_STATSIG_CLIENT_KEY = 'client-test-key';
    mockInitializeAsync.mockClear();
    mockUpdateUserAsync.mockClear();
    mockUseClientAsyncInit.mockClear();
  });

  it('does not initialize Statsig when the SDK key is missing', async () => {
    delete process.env.NEXT_PUBLIC_STATSIG_CLIENT_KEY;

    const { StatsigClientProvider } =
      await import('../../../../app/statsig-provider');

    render(
      <StatsigClientProvider>
        <span>Application shell</span>
      </StatsigClientProvider>
    );

    expect(screen.getByText('Application shell')).toBeInTheDocument();
    expect(mockUseClientAsyncInit).not.toHaveBeenCalled();
  });

  it('initializes Statsig after client mount when the SDK key is set', async () => {
    const { StatsigClientProvider } =
      await import('../../../../app/statsig-provider');

    render(
      <StatsigClientProvider>
        <span>Application shell</span>
      </StatsigClientProvider>
    );

    expect(screen.getByText('Application shell')).toBeInTheDocument();

    await vi.waitFor(() => {
      expect(mockUseClientAsyncInit).toHaveBeenCalledWith(
        'client-test-key',
        expect.objectContaining({ userID: 'anonymous' }),
        expect.objectContaining({ plugins: expect.any(Array) })
      );
    });
  });

  it('trims whitespace from the SDK key before initializing', async () => {
    process.env.NEXT_PUBLIC_STATSIG_CLIENT_KEY = '  client-test-key\n  ';

    const { StatsigClientProvider } =
      await import('../../../../app/statsig-provider');

    render(
      <StatsigClientProvider>
        <span>Application shell</span>
      </StatsigClientProvider>
    );

    await vi.waitFor(() => {
      expect(mockUseClientAsyncInit).toHaveBeenCalledWith(
        'client-test-key',
        expect.any(Object),
        expect.any(Object)
      );
    });
  });

  it('registers AutoCapture and no session-replay plugin (GOO-774)', async () => {
    const { StatsigAutoCapturePlugin } = await import('@statsig/web-analytics');
    const { StatsigClientProvider } =
      await import('../../../../app/statsig-provider');

    render(
      <StatsigClientProvider>
        <span>Application shell</span>
      </StatsigClientProvider>
    );

    await vi.waitFor(() => {
      expect(mockUseClientAsyncInit).toHaveBeenCalled();
    });
    const options = mockUseClientAsyncInit.mock.calls[0][2] as {
      plugins: unknown[];
    };
    // Gates and events stay; the only plugin is AutoCapture. A recorder here
    // would bypass Sentry's replay privacy options.
    expect(options.plugins).toHaveLength(1);
    expect(options.plugins[0]).toBeInstanceOf(StatsigAutoCapturePlugin);
  });

  it('does not initialize Statsig during server rendering', async () => {
    const { StatsigClientProvider } =
      await import('../../../../app/statsig-provider');

    const html = renderToString(
      <StatsigClientProvider>
        <span>Application shell</span>
      </StatsigClientProvider>
    );

    // The server snapshot reports "not mounted", so the SDK (which needs
    // `window`) never starts on the server or during hydration.
    expect(html).toContain('Application shell');
    expect(html).not.toContain('statsig-provider');
    expect(mockUseClientAsyncInit).not.toHaveBeenCalled();
  });
});
