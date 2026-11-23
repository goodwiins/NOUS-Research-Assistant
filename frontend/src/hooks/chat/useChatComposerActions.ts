import { useChatSessionGuard } from './useChatSessionGuard';
import { useCallback } from 'react';
import toast from 'react-hot-toast';

import type { ChatPageMessage } from '@/components/chat/shared/cloudMessageView';
import { enhancedDocumentService } from '@/services/enhancedDocumentService';
import type { MessageAttachment, Workspace } from '@/types/workspace';
import type { PdfBranchIntent } from '@/types/pdf-chat';
import { useChatStore } from '@/store/chat-store';

// ============================================
// HOOK PARAMS
// ============================================

export interface UseChatComposerActionsParams {
  workspace: Workspace | null;
  handleSubmit: (
    contentOverride?: string,
    historyOverride?: ChatPageMessage[],
    supersedesClientMessageId?: string,
    attachmentIds?: string[],
    attachments?: MessageAttachment[],
    branchIntent?: PdfBranchIntent
  ) => Promise<void>;
  /** useChatStreaming's `isBusy`: loading, streaming, a pending confirmation
   * or an active harness run. */
  isBusy: boolean;
  displayedMessages: ChatPageMessage[];
}

/** Whether one attached file made it to storage, and the document it became. */
export interface AttachOutcome {
  ok: boolean;
  /** Present only when `ok`. The composer keeps it on the chip so the id can
   * ride along with the turn the user attached it to. */
  documentId?: string;
}

export interface UseChatComposerActionsReturn {
  /** Per-file upload outcomes, in the order the files were given. */
  handleAttach: (files: FileList) => Promise<AttachOutcome[]>;
  /** Re-send the user turn preceding an assistant message, truncating the
   * transcript to just before it. When that turn carries a persisted
   * `clientMessageId` it rides along as `supersedes_client_message_id`, so the
   * server tombstones the replaced turn instead of persisting a second pair. */
  handleRegenerate: (assistantMessageIndex: number) => void;
  /** Edit a prior user message in place and re-send. Truncates the transcript
   * to just before the edited message (dropping its old answer + any
   * local-only error bubbles) and re-submits with the new text — the same
   * slice-and-resend contract as {@link handleRegenerate}. When the edited turn
   * carries a persisted `clientMessageId`, that id also rides along as
   * `supersedes_client_message_id` so the server durably tombstones the old
   * turn and everything after it. */
  handleEditUserMessage: (userMessageIndex: number, newContent: string) => void;
  retryLast: () => void;
  /** Bare submit — clears nothing, just forwards to the streaming path.
   * Callers that need to clear ephemeral output first (useSlashCommands)
   * wrap this rather than calling handleSubmit directly. */
  submit: (attachmentIds?: string[]) => void;
}

// ============================================
// HOOK
// ============================================

function branchIntentFor(
  message: ChatPageMessage
): PdfBranchIntent | null | undefined {
  const state = useChatStore.getState();
  const threadId = state.currentThreadId;
  if (!threadId || !state.messagePagination[threadId]?.message_branching)
    return undefined;
  if (
    !message.id ||
    message.parentMessageId === undefined ||
    state.messageFreshness[threadId] !== 'fresh' ||
    state.messagePagination[threadId]?.selectingBranch
  )
    return null;
  return {
    parentMessageId: message.parentMessageId,
    replacedMessageId: message.id,
    selectedHeadMessageId:
      state.messagePagination[threadId].selected_message_id ?? null,
  };
}

/**
 * Composer-level actions: attach, retry, regenerate, and submit. Delegates
 * all persistence/streaming to the existing useChatStreaming/store path
 * (handleSubmit) — this hook only coordinates which turn gets (re)sent and
 * uploads files, it never calls a second create-message endpoint.
 */
export function useChatComposerActions({
  workspace,
  handleSubmit,
  isBusy,
  displayedMessages,
}: UseChatComposerActionsParams): UseChatComposerActionsReturn {
  const isCurrentSession = useChatSessionGuard();

  // Upload files selected via the Paperclip attach control.
  // Uses enhancedDocumentService (v1 /files/upload) since no workspace-scoped
  // attach endpoint exists yet. Each upload is isolated via .catch so one
  // failure does not cancel others.
  const handleAttach = useCallback(
    async (files: FileList) => {
      if (!isCurrentSession()) return [];
      if (!workspace) {
        console.warn('[Chat] Cannot attach: no workspace');
        toast.error('No workspace available — attachment was not uploaded.');
        // Every file failed, so the composer's chips must settle on error
        // rather than sit in the uploading state forever.
        return Array.from(files).map(() => ({ ok: false as const }));
      }
      const uploads = Array.from(files).map((file) =>
        enhancedDocumentService
          .uploadDocument(file, {
            title: file.name,
            processing_priority: 'normal',
          })
          .then((result) => ({
            file,
            ok: true as const,
            documentId: result.response.document_id,
          }))
          .catch((err) => {
            // No file name: it is user data, and console.error reaches
            // Sentry (GOO-638).
            console.error('[Chat] Upload failed', {
              errorType: err instanceof Error ? err.name : typeof err,
            });
            return { file, ok: false as const };
          })
      );
      // Promise.all preserves order, so the caller can zip these onto the
      // chips it created from the same FileList.
      const results = await Promise.all(uploads);
      if (!isCurrentSession()) return [];
      const failed = results.filter((r) => !r.ok);
      if (failed.length > 0) {
        toast.error(
          failed.length === 1
            ? `Upload failed for ${failed[0].file.name}.`
            : `Upload failed for ${failed.length} of ${results.length} files.`
        );
      }
      return results.map((r) => ({
        ok: r.ok,
        ...(r.ok && r.documentId ? { documentId: r.documentId } : {}),
      }));
    },
    [workspace, isCurrentSession]
  );

  // Regenerate a specific assistant turn — re-sends its prior user message
  // with the history truncated to just before it. Shared by the /retry
  // command and the ChatMessageList "Regenerate" action.
  const handleRegenerate = useCallback(
    (assistantMessageIndex: number) => {
      if (!isCurrentSession()) return;
      if (isBusy) {
        toast.error('Wait for the current reply to finish before retrying.');
        return;
      }
      let priorUserIndex = -1;
      for (let index = assistantMessageIndex - 1; index >= 0; index -= 1) {
        if (displayedMessages[index]?.role === 'user') {
          priorUserIndex = index;
          break;
        }
      }
      if (priorUserIndex < 0) return;
      const priorUser = displayedMessages[priorUserIndex];
      const regenerationHistory = displayedMessages.slice(0, priorUserIndex);
      // The content goes to handleSubmit explicitly. The composer draft is
      // never touched: a regenerate doesn't own it, so the user's text stays
      // whether the send is accepted or rejected (GOO-751).
      const contentToSend = priorUser.content;
      // Durable regenerate: name the turn being replaced so the server
      // tombstones it, exactly as edit-and-resend does. Without it the
      // regenerated turn persists as an extra pair and the canonical page
      // renders both answers after reconcile/reload. Omitted for legacy rows
      // with no persisted client_message_id (FE-only truncation, as before).
      const supersedes = priorUser.clientMessageId;
      const branch = branchIntentFor(priorUser);
      if (branch === null) {
        toast.error('Wait for this branch to load before retrying.');
        return;
      }
      // Preserve attachments while fencing the deferred send to its account.
      const attachments = priorUser.attachments;
      const attachmentIds = attachments?.map((file) => file.document_id);
      setTimeout(() => {
        if (isCurrentSession())
          void handleSubmit(
            contentToSend,
            regenerationHistory,
            branch ? undefined : supersedes,
            attachmentIds,
            attachments,
            ...(branch ? ([branch] as const) : [])
          );
      }, 0);
    },
    [displayedMessages, handleSubmit, isBusy, isCurrentSession]
  );

  // Edit a prior user message in place and re-send it. The history is
  // truncated to just before the edited message — identical to regenerate — so
  // the edited turn replaces the old answer (and any local-only error bubble).
  // handleSubmit drops non-optimistic overlays from newMessages automatically.
  const handleEditUserMessage = useCallback(
    (userMessageIndex: number, newContent: string) => {
      if (!isCurrentSession()) return;
      if (isBusy) {
        toast.error('Wait for the current reply to finish before editing.');
        return;
      }
      const content = newContent.trim();
      if (!content) return;
      const edited = displayedMessages[userMessageIndex];
      if (!edited || edited.role !== 'user') return;
      const editedHistory = displayedMessages.slice(0, userMessageIndex);
      // Durable edit: name the turn being replaced so the server tombstones it
      // (and everything after it) instead of only truncating the request
      // context. Omitted when the edited turn has no persisted
      // client_message_id — a legacy row or one that never reached the server;
      // the FE-only truncation is then the same behaviour as before.
      const supersedes = edited.clientMessageId;
      const branch = branchIntentFor(edited);
      if (branch === null) {
        toast.error('Wait for this branch to load before editing.');
        return;
      }
      const attachments = edited.attachments;
      const attachmentIds = attachments?.map((file) => file.document_id);
      // The edited text goes to handleSubmit explicitly; the composer draft
      // is left alone, as for regenerate.
      setTimeout(() => {
        if (isCurrentSession())
          void handleSubmit(
            content,
            editedHistory,
            branch ? undefined : supersedes,
            attachmentIds,
            attachments,
            ...(branch ? ([branch] as const) : [])
          );
      }, 0);
    },
    [displayedMessages, handleSubmit, isBusy, isCurrentSession]
  );

  // Regenerate the most recent assistant response (the /retry command).
  const retryLast = useCallback(() => {
    const lastAssistantIdx = [...displayedMessages]
      .map((m, i) => ({ role: m.role, i }))
      .reverse()
      .find((x) => x.role === 'assistant')?.i;
    if (lastAssistantIdx !== undefined) handleRegenerate(lastAssistantIdx);
  }, [displayedMessages, handleRegenerate]);

  const submit = useCallback(
    (attachmentIds?: string[]) => {
      if (isCurrentSession())
        void handleSubmit(undefined, undefined, undefined, attachmentIds);
    },
    [handleSubmit, isCurrentSession]
  );

  return {
    handleAttach,
    handleRegenerate,
    handleEditUserMessage,
    retryLast,
    submit,
  };
}
