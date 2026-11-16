"use client";

import React from "react";
import { Command, Search } from "lucide-react";
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";
import { useReturnFocusOnClose } from "@/hooks/useReturnFocusOnClose";

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

// Radix Dialog supplies role="dialog", aria-modal, Escape, the focus trap and
// focus restore to the element that opened it.
export const KeyboardShortcuts: React.FC<KeyboardShortcutsProps> = ({
  isOpen,
  onClose,
}) => {
  const returnFocus = useReturnFocusOnClose(isOpen);

  // "?" toggles the panel closed while it is open. Stop propagation so the
  // dashboard's window-level "?" handler does not immediately reopen it.
  const handleKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    if (e.key === "?" || (e.key === "/" && e.shiftKey)) {
      e.preventDefault();
      e.stopPropagation();
      onClose();
    }
  };

  return (
    <Dialog
      open={isOpen}
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
    >
      <DialogContent
        aria-describedby={undefined}
        onKeyDown={handleKeyDown}
        onCloseAutoFocus={returnFocus}
        className="max-w-md overflow-hidden rounded-2xl border-amber-200/20 bg-white/95 p-6 backdrop-blur-xl"
      >
        <div className="flex items-center gap-2">
          <Command aria-hidden="true" className="h-5 w-5 text-amber-600" />
          <DialogTitle className="text-lg font-semibold text-gray-800">
            Keyboard Shortcuts
          </DialogTitle>
        </div>

        <div className="space-y-3">
          {shortcuts.map((shortcut) => (
            <div
              key={shortcut.description}
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
                  <React.Fragment key={key}>
                    {keyIndex > 0 && (
                      <span className="text-gray-400 mx-1">+</span>
                    )}
                    <kbd className="px-2 py-1 text-xs font-mono rounded-md bg-gray-100 border border-gray-200 shadow-xs">
                      {key}
                    </kbd>
                  </React.Fragment>
                ))}
              </div>
            </div>
          ))}
        </div>

        <div className="pt-4 border-t border-gray-200">
          <p className="text-xs text-center text-gray-500">
            Press{" "}
            <kbd className="px-1 py-0.5 text-xs font-mono rounded bg-gray-100">
              ESC
            </kbd>{" "}
            to close
          </p>
        </div>
      </DialogContent>
    </Dialog>
  );
};
