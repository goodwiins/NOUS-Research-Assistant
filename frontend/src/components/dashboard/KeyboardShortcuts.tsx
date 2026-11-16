"use client";

import React from "react";
import { motion } from "framer-motion";
import { Command, Search } from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from "@/components/ui/dialog";
import { useDialogReturnFocus } from "@/hooks/useDialogReturnFocus";

interface KeyboardShortcutsProps {
  isOpen: boolean;
  onClose: () => void;
}

interface Shortcut {
  keys: string[];
  description: string;
  icon: React.ReactNode;
}

// Only the shortcuts actually wired up on the dashboard. (Avoid advertising
// ⌘D/⌘F — those collide with the browser's bookmark/find and aren't handled.)
const shortcuts: Shortcut[] = [
  {
    keys: ["⌘", "K"],
    description: "Quick search",
    icon: <Search aria-hidden="true" className="h-4 w-4" />,
  },
  {
    keys: ["?"],
    description: "Show this shortcuts panel",
    icon: <Command aria-hidden="true" className="h-4 w-4" />,
  },
];

// Radix Dialog owns role="dialog", aria-modal, Escape, the focus trap and
// focus restore to whatever opened it (GOO-599).
export const KeyboardShortcuts: React.FC<KeyboardShortcutsProps> = ({
  isOpen,
  onClose,
}) => {
  const restoreFocus = useDialogReturnFocus(isOpen);

  React.useEffect(() => {
    if (!isOpen) return;
    // "?" toggles the panel closed again.
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "?" || (e.key === "/" && e.shiftKey)) {
        e.preventDefault();
        onClose();
      }
    };

    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [isOpen, onClose]);

  return (
    <Dialog
      open={isOpen}
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
    >
      <DialogContent
        onCloseAutoFocus={restoreFocus}
        className="max-w-md gap-0 overflow-hidden rounded-2xl border-amber-200/20 bg-white/95 p-6 backdrop-blur-xl shadow-2xl">
        <div className="pointer-events-none absolute inset-0 bg-linear-to-br from-amber-50/50 to-orange-50/30" />
        <div className="relative">
          <div className="flex items-center gap-2 mb-6 pr-6">
            <Command aria-hidden="true" className="h-5 w-5 text-amber-600" />
            <DialogTitle className="text-lg font-semibold text-gray-800">
              Keyboard Shortcuts
            </DialogTitle>
          </div>
          <DialogDescription className="sr-only">
            Keyboard shortcuts available on the dashboard.
          </DialogDescription>

          <div className="space-y-3">
            {shortcuts.map((shortcut, index) => (
              <motion.div
                key={index}
                initial={{ opacity: 0, x: -20 }}
                animate={{ opacity: 1, x: 0 }}
                transition={{ delay: index * 0.05 }}
                className="flex items-center justify-between p-3 rounded-xl bg-white/50 hover:bg-white/80 transition-colors group"
              >
                <div className="flex items-center gap-3">
                  <div className="p-2 rounded-lg bg-amber-100/50 text-amber-600 group-hover:bg-amber-100 transition-colors">
                    {shortcut.icon}
                  </div>
                  <span className="text-sm font-medium text-gray-700">
                    {shortcut.description}
                  </span>
                </div>
                <div className="flex items-center gap-1">
                  {shortcut.keys.map((key, keyIndex) => (
                    <React.Fragment key={keyIndex}>
                      {keyIndex > 0 && (
                        <span className="text-gray-400 mx-1">+</span>
                      )}
                      <kbd className="px-2 py-1 text-xs font-mono rounded-md bg-gray-100 border border-gray-200 shadow-xs">
                        {key}
                      </kbd>
                    </React.Fragment>
                  ))}
                </div>
              </motion.div>
            ))}
          </div>

          <div className="mt-6 pt-4 border-t border-gray-200">
            <p className="text-xs text-center text-gray-500">
              Press{" "}
              <kbd className="px-1 py-0.5 text-xs font-mono rounded bg-gray-100">
                ESC
              </kbd>{" "}
              to close
            </p>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
};
