import { logger, task } from "@trigger.dev/sdk";
import { backendClient } from "../_lib/backend-client";
import type { ArxivIngestPaperPayload } from "../_lib/schemas";

export const ingestPaper = task({
  id: "arxiv-ingest-paper",
  maxDuration: 300,
  retry: {
    maxAttempts: 3,
    minTimeoutInMs: 10000,
    maxTimeoutInMs: 60000,
    factor: 3,
  },
  run: async (payload: ArxivIngestPaperPayload) => {
    const { paperId } = payload;

    logger.info("Ingesting ArXiv paper", { paperId });

    // POSTed once per attempt (the client does not retry writes). The task
    // retry is the only retry layer; it is safe because the backend reuses an
    // existing document for the same (organization, arXiv id).
    await backendClient.post<Record<string, unknown>>("/api/v1/arxiv/ingest", {
      paper_ids: [paperId],
    });

    // The endpoint queues a FastAPI background task and answers
    // {message, paper_count, status}: no job id and no document id, so there
    // is nothing to poll and the outcome is not observable here. Report the
    // paper as accepted, never as ingested. (GOO-718: a poll of
    // /processing/jobs/{job_id} with a document id used to follow, behind a
    // `document_ids` check the response never satisfied.)
    logger.info("Paper ingest accepted; outcome not observable", { paperId });
    return { status: "accepted" as const, paperId };
  },
});
