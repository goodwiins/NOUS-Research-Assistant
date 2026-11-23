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
        expect.objectContaining({ logLevel: expect.anything() })
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

  it('registers no plugins: no session replay (GOO-774), no AutoCapture (GOO-784)', async () => {
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
      plugins?: unknown[];
    };
    // Gates, experiments and explicit logEvent calls need no plugin. Every
    // Statsig plugin so far sent page URLs, query params, the referrer or
    // element text past Sentry's scrubbing.
    expect(options.plugins ?? []).toEqual([]);
  });

  it('sends Statsig no page URL, query, referrer or first-touch metadata (GOO-784)', async () => {
    window.history.replaceState(
      null,
      '',
      '/search?q=secret-search-text&token_hash=tok-123#access_token=at-456'
    );
    const referrer = vi
      .spyOn(document, 'referrer', 'get')
      .mockReturnValue('https://ref.example/in?q=referrer-leak');
    try {
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
      const [, user, options] = mockUseClientAsyncInit.mock.calls[0];
      const sent = JSON.stringify({ user, options });
      for (const leak of [
        'secret-search-text',
        'tok-123',
        'at-456',
        'ref.example',
        'referrer-leak',
        '/search',
      ]) {
        expect(sent).not.toContain(leak);
      }
      // AutoCapture writes first-touch data onto the user object, where its
      // event filter never sees it.
      expect(user).not.toHaveProperty('analyticsOnlyMetadata');
      const allowedUserKeys = ['userID', 'email', 'customIDs', 'custom'];
      expect(
        Object.keys(user as object).filter(
          (key) => !allowedUserKeys.includes(key)
        )
      ).toEqual([]);
    } finally {
      referrer.mockRestore();
      window.history.replaceState(null, '', '/');
    }
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
