'use client';

import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Loader2 } from 'lucide-react';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { useDialogReturnFocus } from '@/hooks/useDialogReturnFocus';
import { createProject } from '@/services/researchEngineService';
import { listWorkflowLinkOptions } from '@/services/projectService';

export interface CreateProjectModalProps {
  isOpen: boolean;
  onClose: () => void;
  onCreated: () => void;
}

export function CreateProjectModal({
  isOpen,
  onClose,
  onCreated,
}: CreateProjectModalProps) {
  const [collectionId, setCollectionId] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const { data: collections = [] } = useQuery({
    queryKey: ['projects', 'research-engine-link-options'],
    queryFn: listWorkflowLinkOptions,
    enabled: isOpen,
  });
  const restoreFocus = useDialogReturnFocus(isOpen);

  if (!isOpen) return null;
  const selectedCollection = collections.find(
    (collection) => collection.id === collectionId
  );

  const reset = () => {
    setCollectionId('');
    setError(null);
  };

  const handleClose = () => {
    if (submitting) return;
    reset();
    onClose();
  };

  const handleSubmit = async () => {
    if (!collectionId) return;
    if (!selectedCollection) return;
    setSubmitting(true);
    setError(null);
    try {
      await createProject({
        name: selectedCollection.name,
        collection_id: collectionId,
      });
      reset();
      onClose();
      onCreated();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create project');
    } finally {
      setSubmitting(false);
    }
  };

  // Radix Dialog owns role="dialog", aria-modal, Escape, the focus trap and
  // focus restore to the opener (GOO-599).
  return (
    <Dialog
      open={isOpen}
      onOpenChange={(open) => {
        if (!open) handleClose();
      }}
    >
      <DialogContent
        onCloseAutoFocus={restoreFocus}
        className="bg-card border-border max-w-xl"
      >
        <DialogHeader className="mb-2 pr-6">
          <DialogTitle className="text-lg font-mono font-bold text-sol">
            Enable Research Workflow
          </DialogTitle>
          <DialogDescription>
            Pick an existing project to run the research workflow on.
          </DialogDescription>
        </DialogHeader>

        {error && (
          <div className="mb-4 p-3 bg-red-500/10 border border-red-500/30 rounded text-sm text-red-400 font-mono">
            {error}
          </div>
        )}

        <div className="space-y-4">
          <div>
            <label
              htmlFor="engine-project-collection"
              className="block text-xs text-muted-foreground font-mono uppercase tracking-wide mb-1"
            >
              Existing project
            </label>
            <select
              id="engine-project-collection"
              value={collectionId}
              onChange={(event) => setCollectionId(event.target.value)}
              className="w-full px-3 py-2 bg-muted border border-border rounded text-sm text-foreground"
            >
              <option value="">Select a project</option>
              {collections.map((collection) => (
                <option key={collection.id} value={collection.id}>
                  {collection.name}
                </option>
              ))}
            </select>
          </div>
        </div>

        <div className="flex justify-end gap-3 mt-4">
          <button
            type="button"
            onClick={handleClose}
            className="px-4 py-2 text-sm font-mono text-muted-foreground hover:text-foreground transition-colors"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={handleSubmit}
            disabled={!selectedCollection || submitting}
            className="flex items-center gap-2 px-4 py-2 bg-sol/10 text-sol border border-sol/30 rounded font-mono text-sm hover:bg-sol/20 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {submitting && <Loader2 className="h-4 w-4 animate-spin" />}
            Enable Workflow
          </button>
        </div>
      </DialogContent>
    </Dialog>
  );
}

export default CreateProjectModal;
