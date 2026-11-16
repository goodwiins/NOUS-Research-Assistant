"use client";

import React, { useRef, useState } from "react";
import { Search, CornerDownLeft } from "lucide-react";
import { useRouter } from "next/navigation";
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";
import { useReturnFocusOnClose } from "@/hooks/useReturnFocusOnClose";

interface QuickSearchProps {
  isOpen: boolean;
  onClose: () => void;
}

// Radix Dialog supplies role="dialog", aria-modal, Escape, the focus trap and
// focus restore to the element that opened it.
export const QuickSearch: React.FC<QuickSearchProps> = ({ isOpen, onClose }) => {
  const [query, setQuery] = useState("");
  const router = useRouter();
  const inputRef = useRef<HTMLInputElement>(null);
  const returnFocus = useReturnFocusOnClose(isOpen);

  const close = () => {
    onClose();
    setQuery("");
  };

  const runSearch = () => {
    const q = query.trim();
    if (!q) return;
    router.push(`/search?q=${encodeURIComponent(q)}`);
    close();
  };

  return (
    <Dialog
      open={isOpen}
      onOpenChange={(open) => {
        if (!open) close();
      }}
    >
      <DialogContent
        aria-describedby={undefined}
        onOpenAutoFocus={(e) => {
          e.preventDefault();
          inputRef.current?.focus();
        }}
        onCloseAutoFocus={returnFocus}
        className="top-20 max-w-2xl translate-y-0 gap-0 overflow-hidden rounded-2xl border-amber-200/20 bg-white/95 p-0 backdrop-blur-xl data-[state=closed]:slide-out-to-top-0 data-[state=open]:slide-in-from-top-0"
      >
        <DialogTitle className="sr-only">Quick search</DialogTitle>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            runSearch();
          }}
          className="p-4 pr-12 border-b border-gray-200/50"
        >
          <div className="relative">
            <Search
              aria-hidden="true"
              className="absolute left-3 top-1/2 -translate-y-1/2 h-5 w-5 text-gray-400"
            />
            <input
              ref={inputRef}
              type="text"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search all documents…"
              aria-label="Search query"
              className="w-full pl-10 pr-16 py-3 bg-white/50 border border-gray-200/50 rounded-xl focus:outline-hidden focus:ring-2 focus:ring-amber-500/50 focus:border-amber-500/50 transition-all"
            />
            <kbd className="absolute right-3 top-1/2 -translate-y-1/2 px-2 py-1 text-xs font-mono rounded-md bg-gray-100 border border-gray-200">
              ESC
            </kbd>
          </div>
        </form>

        <div className="p-2">
          {query.trim() ? (
            <button
              type="button"
              onClick={runSearch}
              className="w-full p-3 rounded-xl hover:bg-white/50 transition-colors group text-left flex items-center gap-3"
            >
              <div className="p-2 rounded-lg bg-gray-100/50 group-hover:bg-gray-100 transition-colors">
                <Search aria-hidden="true" className="h-4 w-4 text-amber-600" />
              </div>
              <span className="flex-1 min-w-0 truncate font-medium text-gray-800">
                Search documents for “{query.trim()}”
              </span>
              <span className="flex items-center gap-1 text-xs text-gray-500">
                <CornerDownLeft aria-hidden="true" className="h-3 w-3" />
                Enter
              </span>
            </button>
          ) : (
            <p className="p-6 text-center text-sm text-gray-500">
              Type a query and press Enter to search across your documents.
            </p>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
};
