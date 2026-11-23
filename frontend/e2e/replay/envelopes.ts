/**
 * Decode the Sentry envelopes a test captured (GOO-734). Shared by the
 * replay specs; replay_recording payloads may be zlib- or gzip-compressed.
 */
import { gunzipSync, inflateSync } from 'node:zlib';

export interface Captured {
  /** Every envelope, decoded to text (recordings decompressed). */
  text: string[];
  recordings: string[];
}

/** Decode a Sentry envelope; replay_recording payloads may be compressed. */
export function decodeEnvelope(body: Buffer, captured: Captured): void {
  const parts: string[] = [];
  let offset = body.indexOf(0x0a) + 1; // skip envelope header
  parts.push(body.subarray(0, offset).toString('utf8'));
  while (offset < body.length) {
    const headerEnd = body.indexOf(0x0a, offset);
    if (headerEnd < 0) break;
    const headerText = body.subarray(offset, headerEnd).toString('utf8');
    parts.push(headerText);
    const header = JSON.parse(headerText) as { type: string; length?: number };
    const start = headerEnd + 1;
    const end =
      header.length !== undefined
        ? start + header.length
        : (() => {
            const next = body.indexOf(0x0a, start);
            return next < 0 ? body.length : next;
          })();
    const payload = body.subarray(start, end);
    if (header.type === 'replay_recording') {
      const split = payload.indexOf(0x0a);
      const meta = payload.subarray(0, split).toString('utf8');
      const data = payload.subarray(split + 1);
      let recording: string;
      try {
        recording = inflateSync(data).toString('utf8');
      } catch {
        try {
          recording = gunzipSync(data).toString('utf8');
        } catch {
          recording = data.toString('utf8');
        }
      }
      captured.recordings.push(recording);
      parts.push(meta, recording);
    } else {
      parts.push(payload.toString('utf8'));
    }
    offset = end + 1;
  }
  captured.text.push(parts.join('\n'));
}
