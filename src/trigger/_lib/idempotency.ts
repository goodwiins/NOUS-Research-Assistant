import { createHash } from "node:crypto";

/**
 * Deterministic UUID (version 5 layout, SHA-256 based) for a stable seed.
 * The same seed always yields the same UUID, so a retried task attempt sends
 * the same idempotency key as the attempt it replaces.
 */
export function deterministicUuid(seed: string): string {
  const bytes = createHash("sha256").update(seed).digest().subarray(0, 16);
  bytes[6] = (bytes[6] & 0x0f) | 0x50;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = bytes.toString("hex");
  return [
    hex.slice(0, 8),
    hex.slice(8, 12),
    hex.slice(12, 16),
    hex.slice(16, 20),
    hex.slice(20, 32),
  ].join("-");
}

export interface OutgoingMessage {
  role: string;
  content: string;
  client_message_id?: string;
}

/**
 * Stamp the newest user turn with `client_message_id`, the key
 * `POST /api/v1/agent/execute` dedupes on (`agent_runs.idempotency_key`).
 * A key the caller already supplied wins. Messages are copied, not mutated.
 */
export function withClientMessageId(
  messages: ReadonlyArray<OutgoingMessage>,
  clientMessageId: string,
): OutgoingMessage[] {
  const out = messages.map((m) => ({ ...m }));
  for (let i = out.length - 1; i >= 0; i--) {
    if (out[i].role === "user") {
      out[i].client_message_id ??= clientMessageId;
      break;
    }
  }
  return out;
}
