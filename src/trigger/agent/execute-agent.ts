import { logger, task, wait, metadata } from "@trigger.dev/sdk";
import {
  backendClient,
  backendTokenHeaders,
  BackendApiError,
} from "../_lib/backend-client";
import {
  JobStartResponseSchema,
  JobStatusResponseSchema,
  type AgentExecutePayload,
  type JobStatusResponse,
} from "../_lib/schemas";

const MAX_POLL_ATTEMPTS = 120;
const POLL_INTERVAL_SECONDS = 3;
const APPROVAL_ID_RE = /^[a-f0-9]{64}$/;

async function pollUntilDone(
  jobId: string,
  backendToken: string,
  userId: string,
): Promise<{ status: string; result?: Record<string, unknown> }> {
  for (let i = 0; i < MAX_POLL_ATTEMPTS; i++) {
    let job: JobStatusResponse;
    try {
      job = await backendClient.get(
        `/api/v1/agent/jobs/${jobId}`,
        JobStatusResponseSchema,
        backendTokenHeaders(backendToken),
      );
    } catch (err) {
      if (err instanceof BackendApiError && err.status === 404) {
        logger.error("Agent job not found", { jobId });
        return { status: "not_found" };
      }
      throw err;
    }

    metadata.set("backendStatus", job.status);
    if (job.thread_id) metadata.set("threadId", job.thread_id);

    if (job.status === "completed") {
      return { status: "completed", result: job.result ?? undefined };
    }

    if (job.status === "failed") {
      return { status: "failed", result: { error: job.error } };
    }

    if (job.status === "awaiting_confirmation" && job.confirmation) {
      // #1842: /confirm requires the approval receipt the backend issued
      // with this interrupt; a bare {confirmed} body is rejected with 422.
      const approvalId = job.confirmation.approval_id;
      if (typeof approvalId !== "string" || !APPROVAL_ID_RE.test(approvalId)) {
        logger.error("Confirmation carried no valid approval_id", { jobId });
        return {
          status: "failed",
          result: { error: "Confirmation is missing its approval receipt" },
        };
      }

      metadata.set("status", "awaiting_confirmation");
      metadata.set(
        "confirmation",
        JSON.parse(JSON.stringify(job.confirmation)),
      );

      const token = await wait.createToken({
        idempotencyKey: `hitl-${jobId}-${Date.now()}`,
        timeout: "1h",
        tags: [`user:${userId}`, `job:${jobId}`],
      });
      metadata.set("waitTokenId", token.id);

      const decision = await wait
        .forToken<{ confirmed: boolean }>(token)
        .unwrap();

      metadata.set("status", "running");
      metadata.del("confirmation");
      metadata.del("waitTokenId");

      await backendClient.post(
        `/api/v1/agent/confirm/${jobId}`,
        { confirmed: decision.confirmed, approval_id: approvalId },
        undefined,
        backendTokenHeaders(backendToken),
      );

      continue;
    }

    await wait.for({ seconds: POLL_INTERVAL_SECONDS });
  }

  return { status: "timeout" };
}

export const executeAgent = task({
  id: "execute-agent",
  maxDuration: 600,
  retry: {
    maxAttempts: 2,
    minTimeoutInMs: 5000,
    maxTimeoutInMs: 30000,
    factor: 2,
  },
  run: async (payload: AgentExecutePayload) => {
    const { backendToken, userId, ...requestFields } = payload;
    // Keep run metadata bound to the signed subject, not an independently
    // editable payload field. The backend verifies the signature on the call.
    let ticketUserId: unknown;
    try {
      ticketUserId = JSON.parse(
        Buffer.from(backendToken.split(".")[1], "base64url").toString("utf8"),
      ).sub;
    } catch {
      throw new Error("Invalid agent task ticket");
    }
    if (ticketUserId !== userId)
      throw new Error("Agent task identity mismatch");

    metadata.set("userId", userId);
    logger.info("Starting durable agent execution", { userId });
    metadata.set("status", "running");

    const backendRequest = {
      messages: requestFields.messages,
      page_context: requestFields.pageContext,
      model: requestFields.model ?? "",
      use_rag: requestFields.useRag ?? true,
      max_context_docs: requestFields.maxContextDocs ?? 5,
      thread_id: requestFields.threadId,
    };

    const { job_id } = await backendClient.post(
      "/api/v1/agent/execute",
      backendRequest,
      JobStartResponseSchema,
      backendTokenHeaders(backendToken),
    );

    metadata.set("backendJobId", job_id);
    logger.info("Backend job created", { jobId: job_id });

    const result = await pollUntilDone(job_id, backendToken, userId);

    metadata.set("status", result.status);
    logger.info("Agent execution finished", {
      jobId: job_id,
      status: result.status,
    });

    return result;
  },
});
