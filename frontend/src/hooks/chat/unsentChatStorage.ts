'use client';

import type { ChatPageMessage } from '@/components/chat/shared/cloudMessageView';

/**
 * Tab-scoped copy of what the server does not hold (GOO-788): sends it
 * rejected (`sendFailed` bubbles, GOO-772) and the composer draft, per thread.
 * Without it a reload lost both, because the chat store persists only
 * selection ids. sessionStorage, not localStorage: the text never outlives the
 * tab and is never shared between accounts on one machine. Sign-out and an
 * account change remove it (authStore).
 *
 * Storage is best-effort. When it is denied or full, chat keeps working with
 * the in-memory state it has always had.
 */
export const UNSENT_CHAT_STORAGE_KEY = 'nous:chat-unsent:v1';

/** Oldest slots go first, so abandoned threads cannot grow the record. */
const MAX_SLOTS = 20;

interface StoredUnsentTurn {
  runtimeId: string;
  content: string;
  timestamp: number;
  sendFailed: { message: string };
  attachments?: ChatPageMessage['attachments'];
}

interface ThreadSlot {
  updatedAt: number;
  draft?: string;
  unsent?: StoredUnsentTurn[];
}

interface UnsentChatRecord {
  version: 1;
  ownerUserId: string;
  threads: Record<string, ThreadSlot>;
}

function unsentStorage(): Storage | null {
  if (typeof window === 'undefined') return null;
  try {
    return window.sessionStorage;
  } catch {
    // Some browsers expose the property but throw SecurityError while
    // acquiring it under restricted storage policies.
    return null;
  }
}

function removeRecord(storage: Storage): void {
  try {
    storage.removeItem(UNSENT_CHAT_STORAGE_KEY);
  } catch {
    // Denied storage: nothing was kept, so there is nothing to remove.
  }
}

function isUnsentTurn(value: unknown): value is StoredUnsentTurn {
  if (!value || typeof value !== 'object') return false;
  const turn = value as Record<string, unknown>;
  const failure = turn.sendFailed as Record<string, unknown> | null;
  return (
    typeof turn.runtimeId === 'string' &&
    turn.runtimeId.length > 0 &&
    typeof turn.content === 'string' &&
    typeof turn.timestamp === 'number' &&
    Number.isFinite(turn.timestamp) &&
    !!failure &&
    typeof failure === 'object' &&
    typeof failure.message === 'string' &&
    (turn.attachments === undefined || Array.isArray(turn.attachments))
  );
}

function isThreadSlot(value: unknown): value is ThreadSlot {
  if (!value || typeof value !== 'object') return false;
  const slot = value as Record<string, unknown>;
  return (
    typeof slot.updatedAt === 'number' &&
    (slot.draft === undefined || typeof slot.draft === 'string') &&
    (slot.unsent === undefined ||
      (Array.isArray(slot.unsent) && slot.unsent.every(isUnsentTurn)))
  );
}

function isRecord(value: unknown): value is UnsentChatRecord {
  if (!value || typeof value !== 'object') return false;
  const record = value as Record<string, unknown>;
  return (
    record.version === 1 &&
    typeof record.ownerUserId === 'string' &&
    record.ownerUserId.length > 0 &&
    !!record.threads &&
    typeof record.threads === 'object' &&
    !Array.isArray(record.threads) &&
    Object.values(record.threads).every(isThreadSlot)
  );
}

function readRecord(storage: Storage): UnsentChatRecord | null {
  try {
    const raw = storage.getItem(UNSENT_CHAT_STORAGE_KEY);
    if (!raw) return null;
    const parsed: unknown = JSON.parse(raw);
    if (isRecord(parsed)) return parsed;
  } catch {
    // Unreadable or corrupt: drop it below rather than restore garbage.
  }
  removeRecord(storage);
  return null;
}

/** A thread's unsent sends. */
function threadSlot(threadId: string): string {
  return `thread:${threadId}`;
}

/** A composer draft, scoped like the in-memory drafts: by workspace and by
 * thread, with the not-yet-known workspace and the new chat as their own
 * slots. */
function draftSlot(workspaceId: string | null, threadId: string | null): string {
  return `draft:${JSON.stringify([
    workspaceId ?? 'workspace-pending',
    threadId ?? 'new-chat',
  ])}`;
}

function ownedSlot(
  ownerUserId: string,
  key: string
): ThreadSlot | undefined {
  const storage = unsentStorage();
  if (!storage) return undefined;
  const record = readRecord(storage);
  if (!record || record.ownerUserId !== ownerUserId) return undefined;
  return record.threads[key];
}

function updateSlot(
  ownerUserId: string,
  key: string,
  change: (slot: ThreadSlot) => void
): void {
  const storage = unsentStorage();
  if (!storage) return;
  const existing = readRecord(storage);
  // One owner per tab: another account's text is never kept next to this one.
  const record: UnsentChatRecord =
    existing && existing.ownerUserId === ownerUserId
      ? existing
      : { version: 1, ownerUserId, threads: {} };
  const slot: ThreadSlot = { ...record.threads[key], updatedAt: Date.now() };
  change(slot);
  if (slot.draft === undefined && slot.unsent === undefined) {
    delete record.threads[key];
  } else {
    record.threads[key] = slot;
  }
  const keys = Object.keys(record.threads).sort(
    (a, b) => record.threads[a].updatedAt - record.threads[b].updatedAt
  );
  for (const stale of keys.slice(0, Math.max(0, keys.length - MAX_SLOTS))) {
    delete record.threads[stale];
  }
  try {
    if (Object.keys(record.threads).length === 0) {
      storage.removeItem(UNSENT_CHAT_STORAGE_KEY);
    } else {
      storage.setItem(UNSENT_CHAT_STORAGE_KEY, JSON.stringify(record));
    }
  } catch {
    // Quota or denied storage: the in-memory copy still holds the text.
  }
}

/** Mirror a thread's rejected sends. Rows without `sendFailed` are left out:
 * the server holds the canonical ones, and an optimistic row is mid-send. */
export function saveUnsentTurns(
  ownerUserId: string,
  threadId: string,
  messages: readonly ChatPageMessage[]
): void {
  const unsent: StoredUnsentTurn[] = messages.flatMap((message) =>
    message.sendFailed && message.role === 'user'
      ? [
          {
            runtimeId: message.runtimeId,
            content: message.content,
            timestamp: message.timestamp,
            sendFailed: { message: message.sendFailed.message },
            ...(message.attachments?.length
              ? { attachments: message.attachments }
              : {}),
          },
        ]
      : []
  );
  const key = threadSlot(threadId);
  if (!ownedSlot(ownerUserId, key)?.unsent && unsent.length === 0) return;
  updateSlot(ownerUserId, key, (slot) => {
    if (unsent.length > 0) slot.unsent = unsent;
    else delete slot.unsent;
  });
}

/** The rejected sends a reload left behind for this owner's thread. */
export function loadUnsentTurns(
  ownerUserId: string,
  threadId: string
): ChatPageMessage[] {
  return (ownedSlot(ownerUserId, threadSlot(threadId))?.unsent ?? []).map((turn) => ({
    runtimeId: turn.runtimeId,
    source: 'local-only',
    role: 'user',
    content: turn.content,
    timestamp: turn.timestamp,
    sendFailed: { message: turn.sendFailed.message },
    ...(turn.attachments ? { attachments: turn.attachments } : {}),
  }));
}

/** Mirror the composer draft; an empty draft (a send, a clear) removes it. */
export function saveChatDraft(
  ownerUserId: string,
  workspaceId: string | null,
  threadId: string | null,
  draft: string
): void {
  const key = draftSlot(workspaceId, threadId);
  if (!draft && ownedSlot(ownerUserId, key)?.draft === undefined) return;
  updateSlot(ownerUserId, key, (slot) => {
    if (draft) slot.draft = draft;
    else delete slot.draft;
  });
}

export function loadChatDraft(
  ownerUserId: string,
  workspaceId: string | null,
  threadId: string | null
): string | undefined {
  return ownedSlot(ownerUserId, draftSlot(workspaceId, threadId))?.draft || undefined;
}

/** Sign-out: nothing typed in this tab outlives the session. */
export function clearUnsentChatStorage(): void {
  const storage = unsentStorage();
  if (storage) removeRecord(storage);
}

/** Account change: drop another account's text, keep this account's. */
export function discardForeignUnsentChat(userId: string): void {
  const storage = unsentStorage();
  if (!storage) return;
  const record = readRecord(storage);
  if (record && record.ownerUserId !== userId) removeRecord(storage);
}
