import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from '@/components/ui/alert-dialog';
import { cn } from '@/lib/utils';
import * as React from 'react';

interface ConfirmDialogProps {
  trigger: React.ReactNode;
  title: string;
  description: string;
  confirmLabel?: string;
  cancelLabel?: string;
  variant?: 'default' | 'destructive';
  onConfirm: () => void;
  onCancel?: () => void;
  /**
   * Where focus goes after the user confirms. Radix returns focus to the
   * trigger on close, but a confirmed action often disables that trigger
   * while it runs, and a disabled button cannot take focus, so focus falls
   * to <body>. Point this at a stable element (a heading with tabIndex={-1}).
   */
  focusAfterConfirmRef?: React.RefObject<HTMLElement | null>;
}

export function ConfirmDialog({
  trigger,
  title,
  description,
  confirmLabel = 'Confirm',
  cancelLabel = 'Cancel',
  variant = 'default',
  onConfirm,
  onCancel,
  focusAfterConfirmRef,
}: ConfirmDialogProps) {
  const [open, setOpen] = React.useState(false);
  const confirmed = React.useRef(false);

  const handleConfirm = () => {
    confirmed.current = true;
    onConfirm();
    setOpen(false);
  };

  const handleCancel = () => {
    onCancel?.();
    setOpen(false);
  };

  return (
    <AlertDialog open={open} onOpenChange={setOpen}>
      <AlertDialogTrigger asChild>{trigger}</AlertDialogTrigger>
      <AlertDialogContent
        onCloseAutoFocus={(event) => {
          const target = confirmed.current
            ? focusAfterConfirmRef?.current
            : null;
          confirmed.current = false;
          if (target) {
            event.preventDefault();
            target.focus();
          }
        }}
        className="rounded-(--nous-radius-lg) border border-(--nous-border-1) bg-(--nous-bg-1) shadow-(--nous-shadow-lg)"
      >
        <AlertDialogHeader>
          <AlertDialogTitle className="text-(--nous-fg-1) tracking-tight">
            {title}
          </AlertDialogTitle>
          <AlertDialogDescription className="text-(--nous-fg-3) text-xs">
            {description}
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter className="gap-2">
          <AlertDialogCancel
            onClick={handleCancel}
            className="border-(--nous-border-1) bg-(--nous-bg-2) text-(--nous-fg-3) hover:bg-(--nous-bg-3) hover:text-(--nous-fg-1) text-xs"
          >
            {cancelLabel}
          </AlertDialogCancel>
          <AlertDialogAction
            onClick={handleConfirm}
            className={cn(
              'text-xs border transition-all',
              variant === 'destructive'
                ? 'bg-(--nous-mars)/10 border-(--nous-mars)/50 text-(--nous-mars) hover:bg-(--nous-mars)/20 hover:border-(--nous-mars)'
                : 'bg-(--nous-sol)/10 border-(--nous-sol)/50 text-(--nous-sol) hover:bg-(--nous-sol)/20 hover:border-(--nous-sol)'
            )}
          >
            {confirmLabel}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}

// Convenience wrapper for delete actions
export function DeleteConfirmDialog({
  itemName,
  onConfirm,
  children,
}: {
  itemName: string;
  onConfirm: () => void;
  children: React.ReactNode;
}) {
  return (
    <ConfirmDialog
      trigger={children}
      title={`Delete ${itemName}?`}
      description={`This action cannot be undone. This will permanently delete the ${itemName}.`}
      confirmLabel="Delete"
      variant="destructive"
      onConfirm={onConfirm}
    />
  );
}
