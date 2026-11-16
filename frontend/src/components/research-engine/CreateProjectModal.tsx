'use client';

import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Loader2 } from 'lucide-react';
import { createProject } from '@/services/researchEngineService';
import { listWorkflowLinkOptions } from '@/services/projectService';
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogTitle,
} from '@/components/ui/dialog';
import { useReturnFocusOnClose } from '@/hooks/useReturnFocusOnClose';

export interface CreateProjectModalProps {
  isOpen: boolean;
  onClose: () => void;
  onCreated: () => void;
}

// Radix Dialog supplies role="dialog", aria-modal, Escape, the focus trap and
// focus restore to the element that opened it.
export function CreateProjectModal({
  isOpen,
  onClose,
  onCreated,
}: CreateProjectModalProps) {
  const [collectionId, setCollectionId] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const returnFocus = useReturnFocusOnClose(isOpen);

  const { data: collections = [] } = useQuery({
    queryKey: ['projects', 'research-engine-link-options'],
    queryFn: listWorkflowLinkOptions,
    enabled: isOpen,
  });

  const selectedCollection = collections.find(
    (collection) => collection.id === collectionId
  );

  const reset = () => {
    setCollectionId('');
    setError(null);
  };

  // Escape, overlay click and the close button all land here. While a
  // request is in flight the dialog stays open, as before.
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

  return (
    <Dialog
      open={isOpen}
      onOpenChange={(open) => {
        if (!open) handleClose();
      }}
    >
      <DialogContent
        aria-describedby={undefined}
        onCloseAutoFocus={returnFocus}
        className="max-w-xl bg-card border-border"
      >
        <DialogTitle className="font-mono font-bold text-sol">
          Enable Research Workflow
        </DialogTitle>

        {error && (
          <div
            role="alert"
            className="p-3 bg-red-500/10 border border-red-500/30 rounded text-sm text-red-400 font-mono"
          >
            {error}
          </div>
        )}

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

        <DialogFooter className="gap-3 mt-2">
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
            className="flex items-center justify-center gap-2 px-4 py-2 bg-sol/10 text-sol border border-sol/30 rounded font-mono text-sm hover:bg-sol/20 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {submitting && <Loader2 className="h-4 w-4 animate-spin" />}
            Enable Workflow
          </button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export default CreateProjectModal;
