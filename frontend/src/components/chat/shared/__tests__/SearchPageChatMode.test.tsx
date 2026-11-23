import { afterEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { searchService } from '@/services/searchService';
import SearchPage from '../../../../../app/(dashboard)/search/page';

vi.mock('@/lib/analytics', () => ({
  getAnalytics: () => ({
    trackPageView: vi.fn(),
    trackSearch: vi.fn(),
  }),
}));

vi.mock('@/components/chat/shared/ChatBubble', () => ({
  ChatBubble: () => <div>Bubble</div>,
}));

vi.mock('@/components/chat/CitationPanel', () => ({
  CitationPanel: () => <div>Citation Panel</div>,
}));

vi.mock('@/services/searchService', () => ({
  searchService: {
    search: vi.fn(async () => ({ success: false })),
    addToHistory: vi.fn(),
  },
}));

vi.mock('next/navigation', () => ({
  useRouter: () => ({ push: vi.fn() }),
}));

describe('SearchPage chat mode', () => {
  it('renders terminal chat composer placeholder', async () => {
    render(<SearchPage />);

    expect(
      await screen.findByPlaceholderText(/message nous/i)
    ).toBeInTheDocument();
  });

  afterEach(() => {
    window.history.replaceState(null, '', '/');
  });

  it('runs a ?q= hand-off, then drops the query from the URL (GOO-734)', async () => {
    // jsdom has no layout: the page scrolls to the newest message.
    Element.prototype.scrollIntoView = vi.fn();
    window.history.replaceState({ kept: true }, '', '/search?q=lithium%20dose&tab=x');
    render(<SearchPage />);

    await waitFor(() =>
      expect(searchService.search).toHaveBeenCalledWith(
        { query: 'lithium dose', limit: 10 },
        expect.any(AbortSignal)
      )
    );
    expect(window.location.pathname + window.location.search).toBe('/search?tab=x');
    expect(window.location.href).not.toContain('lithium');
    expect(window.history.state).toEqual({ kept: true });
  });
});
