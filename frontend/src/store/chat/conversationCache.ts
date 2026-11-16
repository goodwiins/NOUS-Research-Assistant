/**
 * Local-only cascade for a conversation that is gone (deleted by the user, or
 * reported 404 by the server). Shared by `deleteConversation` and the
 * `loadThreads` 404 recovery so both leave the same cache end state. Makes no
 * server call.
 *
 * Only a real delete (`reason: 'deleted'`) records the threads in the
 * session-scoped `deletedThreadIds` set, which makes `refreshMessages` and
 * `loadOlderMessages` refuse them until a full store reset. A 404 is not proof
 * of deletion — the backend also reports access-denied as 404, and access can
 * come back (org scope restored, a new conversation becoming readable) — so
 * `reason: 'not-found'` clears the caches without that permanent block.
 */
import { removeItemFromRecord } from './recordIndex';
import {
  abortNewestPageRequest,
  markThreadDeleted,
} from './requestCoordinator';
import type { ChatActions, ChatState } from './types';

export type DropConversationReason = 'deleted' | 'not-found';

export function dropConversationLocally(
  get: () => ChatState & ChatActions,
  set: (fn: (state: ChatState) => void) => void,
  id: string,
  reason: DropConversationReason
): void {
  // Cascade: the conversation's threads (and their message caches) are
  // unreachable once it's gone — deleting only the conversation row used
  // to orphan cached threads/messages/pagination/freshness and their
  // reverse-index entries, and left descendant requests in flight.
  const snapshot = get();
  const threadIds = new Set([
    ...(snapshot.threads[id] || []).map((t) => t.id),
    ...Object.keys(snapshot.threadToConversation).filter(
      (threadId) => snapshot.threadToConversation[threadId] === id
    ),
  ]);
  if (reason === 'deleted') {
    threadIds.forEach(markThreadDeleted);
  }
  threadIds.forEach(abortNewestPageRequest);
  set((state) => {
    // Use O(1) reverse index lookup (GOO-86); fall back to a scan when the
    // index has no entry for it (a 404'd conversation may never have had one)
    if (
      !removeItemFromRecord(
        state.conversations,
        id,
        state.conversationToWorkspace
      )
    ) {
      removeItemFromRecord(state.conversations, id);
    }
    delete state.conversationToWorkspace[id];
    for (const threadId of threadIds) {
      for (const message of state.messages[threadId] || []) {
        delete state.messageToThread[message.id];
      }
      delete state.messages[threadId];
      delete state.messagePagination[threadId];
      delete state.messageFreshness[threadId];
      delete state.threadToConversation[threadId];
      if (state.currentThreadId === threadId) {
        state.currentThreadId = null;
      }
    }
    delete state.threads[id];
    if (state.currentConversationId === id) {
      state.currentConversationId = null;
      state.currentThreadId = null;
    }
  });
}
