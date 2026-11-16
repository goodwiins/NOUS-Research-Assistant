import { logger } from "@trigger.dev/sdk";
import { z } from "zod";
import { createHmac } from "node:crypto";

const BACKEND_API_URL = process.env.BACKEND_API_URL || "https://localhost:8000";

const MAX_RETRIES = 2;
const RETRY_DELAY_MS = 1000;
const REQUEST_TIMEOUT_MS = 30_000;

export class BackendApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly body: unknown,
    url: string,
  ) {
    super(`Backend API ${status} from ${url}`);
    this.name = "BackendApiError";
  }
}

async function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function request<T>(
  method: string,
  path: string,
  options: {
    body?: unknown;
    schema?: z.ZodType<T>;
    headers?: Record<string, string>;
    timeoutMs?: number;
  } = {},
): Promise<T> {
  const url = `${BACKEND_API_URL}${path}`;
  const {
    body,
    schema,
    headers = {},
    timeoutMs = REQUEST_TIMEOUT_MS,
  } = options;

  let lastError: Error | undefined;

  for (let attempt = 0; attempt <= MAX_RETRIES; attempt++) {
    if (attempt > 0) {
      const delay = RETRY_DELAY_MS * Math.pow(2, attempt - 1);
      logger.info(`Retrying ${method} ${path} (attempt ${attempt + 1})`, {
        delay,
      });
      await sleep(delay);
    }

    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), timeoutMs);

    try {
      const res = await fetch(url, {
        method,
        headers: {
          "Content-Type": "application/json",
          ...headers,
        },
        body: body ? JSON.stringify(body) : undefined,
        signal: controller.signal,
      });

      clearTimeout(timeout);

      if (res.status >= 500 && attempt < MAX_RETRIES) {
        lastError = new BackendApiError(res.status, await res.text(), url);
        continue;
      }

      if (!res.ok) {
        throw new BackendApiError(res.status, await res.text(), url);
      }

      const json = await res.json();
      return schema ? schema.parse(json) : (json as T);
    } catch (err) {
      clearTimeout(timeout);

      if (err instanceof BackendApiError) throw err;

      lastError = err instanceof Error ? err : new Error(String(err));
      if (attempt === MAX_RETRIES) break;
    }
  }

  throw lastError ?? new Error(`Failed after ${MAX_RETRIES + 1} attempts`);
}

function authHeaders(method: string, path: string): Record<string, string> {
  const key = process.env.TRIGGER_SERVICE_SIGNING_KEY;
  const userId = process.env.TRIGGER_SERVICE_USER_ID;
  if (!key || Buffer.byteLength(key) < 32 || !userId) {
    throw new Error("Trigger backend service identity is not configured");
  }
  const now = Math.floor(Date.now() / 1000);
  const header = Buffer.from(
    JSON.stringify({ alg: "HS256", typ: "JWT" }),
  ).toString("base64url");
  const claims = Buffer.from(
    JSON.stringify({
      iss: "nous-trigger",
      aud: "nous-backend",
      sub: userId,
      scope: "trigger:service",
      method,
      path,
      iat: now,
      exp: now + 120,
    }),
  ).toString("base64url");
  const signingInput = `${header}.${claims}`;
  const signature = createHmac("sha256", key)
    .update(signingInput)
    .digest("base64url");
  return { Authorization: `Bearer ${signingInput}.${signature}` };
}

export function backendTokenHeaders(token: string): Record<string, string> {
  return { Authorization: `Bearer ${token}` };
}

export const backendClient = {
  get<T>(
    path: string,
    schema?: z.ZodType<T>,
    extraHeaders?: Record<string, string>,
  ) {
    return request<T>("GET", path, {
      schema,
      headers: extraHeaders?.Authorization
        ? extraHeaders
        : { ...authHeaders("GET", path), ...extraHeaders },
    });
  },

  post<T>(
    path: string,
    body: unknown,
    schema?: z.ZodType<T>,
    extraHeaders?: Record<string, string>,
  ) {
    return request<T>("POST", path, {
      body,
      schema,
      headers: extraHeaders?.Authorization
        ? extraHeaders
        : { ...authHeaders("POST", path), ...extraHeaders },
    });
  },

  healthCheck() {
    return request("GET", "/health");
  },
};
