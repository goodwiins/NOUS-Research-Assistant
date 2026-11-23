import { AlertTriangle } from 'lucide-react';

interface ProcessedWithWarningsProps {
  warnings: string[];
  /** Already formatted for display. */
  completedAt?: string | null;
}

/**
 * Status line for a document that finished processing without some optional
 * output (GOO-744). It replaces the plain "Indexed" confirmation so a partial
 * run never reads as a clean one, and lists each step's warning.
 *
 * There is deliberately no Reprocess action: a forced reprocess runs the
 * ingestion pipeline, which neither re-runs the failed optional steps nor
 * clears the warnings, and takes the document out of search meanwhile
 * (GOO-744 review; a real "re-run failed steps" action belongs to GOO-700).
 */
export function ProcessedWithWarnings({
  warnings,
  completedAt,
}: ProcessedWithWarningsProps): React.JSX.Element {
  return (
    <section
      aria-label="Processing status"
      className="rounded-xl border border-border bg-card px-5 py-3 shadow-xs"
    >
      <div className="flex items-center justify-between gap-3">
        <div className="flex min-w-0 items-center gap-2.5">
          {/* fg-accent-safe: 4.89:1 on white (sol-safe), helios on dark. */}
          <AlertTriangle
            aria-hidden="true"
            className="h-4 w-4 shrink-0 text-(--nous-fg-accent-safe)"
          />
          <span className="text-sm font-medium text-foreground">
            Processed with warnings
          </span>
          <span className="truncate text-xs text-muted-foreground">
            searchable, but some steps didn&apos;t finish
          </span>
        </div>
        {completedAt && (
          <span className="shrink-0 text-xs text-muted-foreground tabular-nums">
            {completedAt}
          </span>
        )}
      </div>
      {warnings.length > 0 ? (
        <ul
          aria-label="Processing warnings"
          className="mt-2 list-disc space-y-1 pl-9 text-xs text-muted-foreground"
        >
          {warnings.map((warning, index) => (
            <li key={`${index}-${warning}`}>{warning}</li>
          ))}
        </ul>
      ) : (
        <p className="mt-2 pl-6.5 text-xs text-muted-foreground">
          What didn&apos;t finish wasn&apos;t recorded.
        </p>
      )}
    </section>
  );
}
