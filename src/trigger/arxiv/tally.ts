/**
 * Outcome counts for an arXiv bulk ingest. A child run that finished without
 * throwing is not automatically a success. `ingest-paper` returns `accepted`:
 * the backend queues the paper and reports nothing to watch (GOO-718). Only an
 * explicit `completed` counts as ingested; any outcome the tally does not know
 * is a failure.
 */
export interface IngestTally {
  ingested: number;
  accepted: number;
  skipped: number;
  failed: number;
}

export type ChildRun =
  | { ok: true; output?: unknown }
  | { ok: false; error?: unknown };

export function emptyTally(): IngestTally {
  return { ingested: 0, accepted: 0, skipped: 0, failed: 0 };
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
    if (status === "completed") into.ingested++;
    else if (status === "accepted") into.accepted++;
    else if (status === "skipped") into.skipped++;
    else into.failed++;
  }
  return into;
}
