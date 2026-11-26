/**
 * Outcome counts for an arXiv bulk ingest. `ingest-paper` either returns
 * `accepted` (the backend queued the paper and reports nothing to watch,
 * GOO-718) or throws. A child run that finished without throwing is not
 * automatically a success: only an explicit `accepted` counts, and anything
 * else, including a failed run, is a failure.
 */
export interface IngestTally {
  accepted: number;
  failed: number;
}

export type ChildRun =
  | { ok: true; output?: unknown }
  | { ok: false; error?: unknown };

export function emptyTally(): IngestTally {
  return { accepted: 0, failed: 0 };
}

export type BulkIngestStatus = "completed" | "partial" | "failed";

/**
 * Overall outcome of a bulk ingest (GOO-764). `completed` only when nothing
 * failed; `failed` when every paper failed; `partial` otherwise. An empty
 * search (nothing to ingest) is `completed`.
 */
export function bulkIngestStatus(tally: IngestTally): BulkIngestStatus {
  if (tally.failed === 0) return "completed";
  return tally.accepted === 0 ? "failed" : "partial";
}

/** Add `runs` to `into` (mutated and returned). Unknown outcomes are failures. */
export function tallyIngestRuns(
  runs: ReadonlyArray<ChildRun>,
  into: IngestTally = emptyTally(),
): IngestTally {
  for (const run of runs) {
    const status =
      run.ok && run.output && typeof run.output === "object"
        ? (run.output as { status?: unknown }).status
        : undefined;
    if (status === "accepted") into.accepted++;
    else into.failed++;
  }
  return into;
}
