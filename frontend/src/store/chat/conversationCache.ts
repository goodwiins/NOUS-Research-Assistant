/**
 * Local-only cascade for a conversation that is gone (deleted by the user, or
 * reported 404 by the server). Shared by `deleteConversation` and the
 * `loadThreads` 404 recovery so both leave the same end state. Makes no
 * server call.
 */
import { removeItemFromRecord } from './recordIndex';
import {
  abortNewestPageRequest,
  markThreadDeleted,
} from './requestCoordinator';
import type { ChatActions, ChatState } from './types';

export function dropConversationLocally(
  get: () => ChatState & ChatActions,
  set: (fn: (state: ChatState) => void) => void,
  id: string
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
  threadIds.forEach(markThreadDeleted);
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
