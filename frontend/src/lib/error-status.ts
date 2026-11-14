/**
 * Read an HTTP status from any error shape the frontend can see:
 * `APIErrorClass` (`error.status_code`, thrown by `@/services/api-client`),
 * axios-style (`response.status`), or a flat `status_code`.
 */
export function getErrorStatus(error: unknown): number | undefined {
  if (typeof error !== 'object' || error === null) return undefined;
  const candidate = error as {
    error?: { status_code?: unknown };
    response?: { status?: unknown };
    status_code?: unknown;
  };
  const status =
    candidate.error?.status_code ??
    candidate.response?.status ??
    candidate.status_code;
  return typeof status === 'number' ? status : undefined;
}
