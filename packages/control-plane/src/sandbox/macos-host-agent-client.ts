/**
 * REST client for the Mac host agent (packages/macos-infra).
 *
 * Shaped like the OpenComputer client because the problem is the same one: a
 * self-hosted sandbox API reached over a configurable base URL. The paths are
 * overridable for the same reason too — the host agent is ours, so a route can
 * move without a control-plane release having to follow it.
 *
 * The status codes it returns are part of the contract rather than incidental.
 * A host at its VM limit answers 503, which the shared classifier already
 * treats as transient, so capacity exhaustion cannot trip the spawn circuit
 * breaker. Everything that survives to the caller as an error carries its
 * status for exactly that decision.
 */

import { z } from "zod";
import { createLogger } from "../logger";

const log = createLogger("macos-host-agent-client");

export interface MacosHostAgentConfig {
  /** Host agent base URL — in production the Cloudflare Tunnel hostname. */
  apiUrl: string;
  /** Pre-shared key. Also the HMAC secret for editor and desktop passwords. */
  apiKey: string;
  /** Defaults to the header the host agent reads. */
  authHeaderName?: string;
  /** Route overrides, for a host agent that has moved an endpoint. */
  paths?: Partial<MacosHostAgentPaths>;
}

export interface MacosHostAgentPaths {
  sandboxes: string;
  sandbox: string;
  exec: string;
  suspend: string;
  resume: string;
  timeout: string;
}

const DEFAULT_PATHS: MacosHostAgentPaths = {
  sandboxes: "/sandboxes",
  sandbox: "/sandboxes/:id",
  exec: "/sandboxes/:id/exec",
  suspend: "/sandboxes/:id/suspend",
  resume: "/sandboxes/:id/resume",
  timeout: "/sandboxes/:id/timeout",
};

export const DEFAULT_AUTH_HEADER_NAME = "X-OI-Host-Agent-Key";

/**
 * Creating a sandbox waits for the host to clone a golden image and boot it,
 * which is the one call here that can legitimately take minutes.
 */
const TIMEOUT_CREATE_MS = 180_000;
const TIMEOUT_RESUME_MS = 120_000;
const TIMEOUT_SUSPEND_MS = 60_000;
const TIMEOUT_DELETE_MS = 60_000;
const TIMEOUT_GET_MS = 15_000;
const TIMEOUT_TIMEOUT_MS = 15_000;
const TIMEOUT_EXEC_MS = 60_000;

const sandboxSchema = z.object({
  sandbox_id: z.string(),
  status: z.string(),
  created_at_ms: z.number(),
  expires_at_ms: z.number(),
  timeout_seconds: z.number(),
});

const execSchema = z.object({
  exit_code: z.number(),
  stdout: z.string(),
  stderr: z.string(),
});

export type MacosSandbox = z.infer<typeof sandboxSchema>;
export type MacosExecResult = z.infer<typeof execSchema>;

export interface MacosCreateSandboxRequest {
  sandboxId: string;
  /** The shared sandbox environment. Carries the session token; never argv. */
  environment: Record<string, string>;
  timeoutSeconds: number;
  /** Golden image to clone. Ignored by a host agent that does not virtualize. */
  image?: string;
}

export class MacosHostAgentNotFoundError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "MacosHostAgentNotFoundError";
  }
}

export class MacosHostAgentApiError extends Error {
  constructor(
    message: string,
    public readonly status: number
  ) {
    super(message);
    this.name = "MacosHostAgentApiError";
  }
}

type HttpMethod = "GET" | "POST" | "DELETE";

interface RequestOptions {
  body?: unknown;
  signal?: AbortSignal;
}

export class MacosHostAgentClient {
  private readonly baseUrl: string;
  private readonly paths: MacosHostAgentPaths;

  constructor(public readonly config: MacosHostAgentConfig) {
    if (!config.apiUrl) throw new Error("MacosHostAgentClient requires apiUrl");
    if (!config.apiKey) throw new Error("MacosHostAgentClient requires apiKey");
    this.baseUrl = config.apiUrl.replace(/\/+$/, "");
    this.paths = { ...DEFAULT_PATHS, ...(config.paths ?? {}) };
  }

  async createSandbox(request: MacosCreateSandboxRequest): Promise<MacosSandbox> {
    const body: Record<string, unknown> = {
      sandbox_id: request.sandboxId,
      environment: request.environment,
      timeout_seconds: request.timeoutSeconds,
    };
    if (request.image) body.image = request.image;

    const sandbox = await this.requestJson(
      "POST",
      this.paths.sandboxes,
      TIMEOUT_CREATE_MS,
      sandboxSchema,
      { body }
    );
    log.info("Created macOS sandbox", {
      sandbox_id: sandbox.sandbox_id,
      status: sandbox.status,
    });
    return sandbox;
  }

  getSandbox(id: string, signal?: AbortSignal): Promise<MacosSandbox> {
    return this.requestJson(
      "GET",
      this.expandPath(this.paths.sandbox, { id }),
      TIMEOUT_GET_MS,
      sandboxSchema,
      { signal }
    );
  }

  suspendSandbox(id: string, signal?: AbortSignal): Promise<MacosSandbox> {
    return this.requestJson(
      "POST",
      this.expandPath(this.paths.suspend, { id }),
      TIMEOUT_SUSPEND_MS,
      sandboxSchema,
      { signal }
    );
  }

  resumeSandbox(id: string, signal?: AbortSignal): Promise<MacosSandbox> {
    return this.requestJson(
      "POST",
      this.expandPath(this.paths.resume, { id }),
      TIMEOUT_RESUME_MS,
      sandboxSchema,
      { signal }
    );
  }

  setTimeout(id: string, timeoutSeconds: number, signal?: AbortSignal): Promise<MacosSandbox> {
    return this.requestJson(
      "POST",
      this.expandPath(this.paths.timeout, { id }),
      TIMEOUT_TIMEOUT_MS,
      sandboxSchema,
      { body: { timeout_seconds: timeoutSeconds }, signal }
    );
  }

  execute(
    id: string,
    argv: string[],
    options?: { environment?: Record<string, string>; signal?: AbortSignal }
  ): Promise<MacosExecResult> {
    return this.requestJson(
      "POST",
      this.expandPath(this.paths.exec, { id }),
      TIMEOUT_EXEC_MS,
      execSchema,
      { body: { argv, environment: options?.environment ?? {} }, signal: options?.signal }
    );
  }

  /** Idempotent on the host agent's side: deleting a reaped sandbox succeeds. */
  deleteSandbox(id: string, signal?: AbortSignal): Promise<void> {
    return this.send(
      "DELETE",
      this.expandPath(this.paths.sandbox, { id }),
      TIMEOUT_DELETE_MS,
      { signal },
      () => {}
    );
  }

  private getHeaders(): Record<string, string> {
    return {
      "Content-Type": "application/json",
      [this.config.authHeaderName ?? DEFAULT_AUTH_HEADER_NAME]: this.config.apiKey,
    };
  }

  private requestJson<T>(
    method: HttpMethod,
    path: string,
    timeoutMs: number,
    schema: z.ZodType<T>,
    options?: RequestOptions
  ): Promise<T> {
    return this.send(method, path, timeoutMs, options, async (response) =>
      this.parseJson(schema, await response.text(), response.status)
    );
  }

  /**
   * A body that does not parse is a protocol violation, reported as one rather
   * than surfacing as a sandbox with missing fields.
   */
  private parseJson<T>(schema: z.ZodType<T>, text: string, status: number): T {
    let payload: unknown;
    try {
      payload = JSON.parse(text);
    } catch {
      throw new MacosHostAgentApiError("Invalid macOS host agent response", status);
    }

    const parsed = schema.safeParse(payload);
    if (!parsed.success) {
      throw new MacosHostAgentApiError("Invalid macOS host agent response", status);
    }
    return parsed.data;
  }

  private async send<T>(
    method: HttpMethod,
    path: string,
    timeoutMs: number,
    options: RequestOptions | undefined,
    consume: (response: Response) => T | Promise<T>
  ): Promise<T> {
    const controller = new AbortController();
    const timeoutId = globalThis.setTimeout(() => controller.abort(), timeoutMs);

    try {
      const externalSignal = options?.signal;
      const init: RequestInit = {
        method,
        headers: this.getHeaders(),
        signal: externalSignal
          ? AbortSignal.any([controller.signal, externalSignal])
          : controller.signal,
      };
      if (options?.body !== undefined) init.body = JSON.stringify(options.body);

      const response = await fetch(`${this.baseUrl}${path}`, init);

      if (response.status === 404) {
        const text = await response.text();
        throw new MacosHostAgentNotFoundError(text || `Not found: ${path}`);
      }

      if (!response.ok) {
        const text = await response.text();
        throw new MacosHostAgentApiError(text || response.statusText, response.status);
      }

      return await consume(response);
    } catch (error) {
      // The message has to contain "timeout" so the shared classifier reads it
      // as transient (isTransientNetworkError). A Mac on the far end of a
      // tunnel is exactly where a slow response should be retried rather than
      // counted against the circuit breaker.
      if (error instanceof Error && error.name === "AbortError") {
        throw new Error(
          `macOS host agent request timeout after ${timeoutMs}ms (${method} ${path})`
        );
      }
      throw error;
    } finally {
      globalThis.clearTimeout(timeoutId);
    }
  }

  private expandPath(path: string, params: Record<string, string>): string {
    let expanded = path;
    for (const [key, value] of Object.entries(params)) {
      expanded = expanded.replace(`:${key}`, encodeURIComponent(value));
    }
    return expanded;
  }
}

export function createMacosHostAgentClient(config: MacosHostAgentConfig): MacosHostAgentClient {
  return new MacosHostAgentClient(config);
}
