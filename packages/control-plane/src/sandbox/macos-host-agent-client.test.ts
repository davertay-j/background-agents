import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  DEFAULT_AUTH_HEADER_NAME,
  MacosHostAgentApiError,
  MacosHostAgentClient,
  MacosHostAgentNotFoundError,
} from "./macos-host-agent-client";

let fetchSpy: ReturnType<typeof vi.fn>;

beforeEach(() => {
  fetchSpy = vi.fn();
  vi.stubGlobal("fetch", fetchSpy);
});

afterEach(() => {
  vi.restoreAllMocks();
});

const SANDBOX_BODY = {
  sandbox_id: "sandbox-1",
  status: "running",
  created_at_ms: 1_700_000_000_000,
  expires_at_ms: 1_700_000_007_200_000,
  timeout_seconds: 7200,
};

function respond(body: unknown, status = 200): Response {
  return new Response(typeof body === "string" ? body : JSON.stringify(body), { status });
}

function createClient(
  overrides: Partial<ConstructorParameters<typeof MacosHostAgentClient>[0]> = {}
) {
  return new MacosHostAgentClient({
    apiUrl: "https://mac-mini-5.example",
    apiKey: "host-agent-key",
    ...overrides,
  });
}

function requestOf(call: number) {
  const [url, init] = fetchSpy.mock.calls[call] as [string, RequestInit];
  return { url, init, headers: init.headers as Record<string, string> };
}

describe("MacosHostAgentClient", () => {
  describe("configuration", () => {
    it("requires a base URL and a key", () => {
      expect(() => createClient({ apiUrl: "" })).toThrow("requires apiUrl");
      expect(() => createClient({ apiKey: "" })).toThrow("requires apiKey");
    });

    it("tolerates a trailing slash on the base URL", async () => {
      fetchSpy.mockResolvedValue(respond(SANDBOX_BODY));

      await createClient({ apiUrl: "https://mac-mini-5.example/" }).getSandbox("sandbox-1");

      expect(requestOf(0).url).toBe("https://mac-mini-5.example/sandboxes/sandbox-1");
    });

    it("presents the key on every request", async () => {
      fetchSpy.mockResolvedValue(respond(SANDBOX_BODY));

      await createClient().getSandbox("sandbox-1");

      expect(requestOf(0).headers[DEFAULT_AUTH_HEADER_NAME]).toBe("host-agent-key");
    });

    it("can name a different auth header", async () => {
      fetchSpy.mockResolvedValue(respond(SANDBOX_BODY));

      await createClient({ authHeaderName: "X-Api-Key" }).getSandbox("sandbox-1");

      expect(requestOf(0).headers["X-Api-Key"]).toBe("host-agent-key");
    });

    it("can override a route the host agent has moved", async () => {
      fetchSpy.mockResolvedValue(respond(SANDBOX_BODY));

      await createClient({ paths: { sandbox: "/v2/sandboxes/:id" } }).getSandbox("sandbox-1");

      expect(requestOf(0).url).toBe("https://mac-mini-5.example/v2/sandboxes/sandbox-1");
    });

    it("encodes the sandbox id into the path", async () => {
      fetchSpy.mockResolvedValue(respond(SANDBOX_BODY));

      await createClient().getSandbox("sandbox/../etc");

      expect(requestOf(0).url).toBe("https://mac-mini-5.example/sandboxes/sandbox%2F..%2Fetc");
    });
  });

  describe("createSandbox", () => {
    it("posts the environment, the lifetime, and the image", async () => {
      fetchSpy.mockResolvedValue(respond(SANDBOX_BODY));

      const sandbox = await createClient().createSandbox({
        sandboxId: "sandbox-1",
        environment: { SANDBOX_AUTH_TOKEN: "token" },
        timeoutSeconds: 7200,
        image: "openinspect/macos-xcode:26",
      });

      expect(sandbox.sandbox_id).toBe("sandbox-1");
      const { url, init } = requestOf(0);
      expect(url).toBe("https://mac-mini-5.example/sandboxes");
      expect(init.method).toBe("POST");
      expect(JSON.parse(init.body as string)).toEqual({
        sandbox_id: "sandbox-1",
        environment: { SANDBOX_AUTH_TOKEN: "token" },
        timeout_seconds: 7200,
        image: "openinspect/macos-xcode:26",
      });
    });

    it("omits the image when the deployment has not configured one", async () => {
      fetchSpy.mockResolvedValue(respond(SANDBOX_BODY));

      await createClient().createSandbox({
        sandboxId: "sandbox-1",
        environment: {},
        timeoutSeconds: 7200,
      });

      expect(JSON.parse(requestOf(0).init.body as string)).not.toHaveProperty("image");
    });
  });

  describe("lifecycle calls", () => {
    it.each([
      ["suspendSandbox", "POST", "/sandboxes/sandbox-1/suspend"],
      ["resumeSandbox", "POST", "/sandboxes/sandbox-1/resume"],
    ] as const)("%s issues %s %s", async (method, httpMethod, path) => {
      fetchSpy.mockResolvedValue(respond(SANDBOX_BODY));

      await createClient()[method]("sandbox-1");

      const { url, init } = requestOf(0);
      expect(url).toBe(`https://mac-mini-5.example${path}`);
      expect(init.method).toBe(httpMethod);
    });

    it("sets a new lifetime", async () => {
      fetchSpy.mockResolvedValue(respond({ ...SANDBOX_BODY, timeout_seconds: 900 }));

      const sandbox = await createClient().setTimeout("sandbox-1", 900);

      expect(sandbox.timeout_seconds).toBe(900);
      expect(JSON.parse(requestOf(0).init.body as string)).toEqual({ timeout_seconds: 900 });
    });

    it("accepts an empty body when deleting", async () => {
      fetchSpy.mockResolvedValue(new Response(null, { status: 204 }));

      await expect(createClient().deleteSandbox("sandbox-1")).resolves.toBeUndefined();
      expect(requestOf(0).init.method).toBe("DELETE");
    });

    it("runs a command and reports its outcome", async () => {
      fetchSpy.mockResolvedValue(respond({ exit_code: 3, stdout: "out", stderr: "err" }));

      const result = await createClient().execute("sandbox-1", ["sw_vers"]);

      expect(result).toEqual({ exit_code: 3, stdout: "out", stderr: "err" });
      expect(JSON.parse(requestOf(0).init.body as string)).toEqual({
        argv: ["sw_vers"],
        environment: {},
      });
    });
  });

  describe("failures", () => {
    it("distinguishes a missing sandbox from every other failure", async () => {
      fetchSpy.mockResolvedValue(respond("No sandbox sandbox-1", 404));

      await expect(createClient().getSandbox("sandbox-1")).rejects.toBeInstanceOf(
        MacosHostAgentNotFoundError
      );
    });

    it("carries the status through, because the caller classifies on it", async () => {
      fetchSpy.mockResolvedValue(respond("Both VM slots are in use", 503));

      await expect(createClient().getSandbox("sandbox-1")).rejects.toMatchObject({
        name: "MacosHostAgentApiError",
        status: 503,
        message: "Both VM slots are in use",
      });
    });

    it("reports a body that is not JSON as a protocol violation", async () => {
      fetchSpy.mockResolvedValue(respond("<html>proxy error</html>"));

      await expect(createClient().getSandbox("sandbox-1")).rejects.toBeInstanceOf(
        MacosHostAgentApiError
      );
    });

    it("reports a sandbox missing its fields rather than passing it on", async () => {
      fetchSpy.mockResolvedValue(respond({ sandbox_id: "sandbox-1" }));

      await expect(createClient().getSandbox("sandbox-1")).rejects.toThrow(
        "Invalid macOS host agent response"
      );
    });

    it("says timeout when it gives up, so the caller retries instead of counting it", async () => {
      fetchSpy.mockImplementation(async (_url: string, init: RequestInit) => {
        // Never settles on its own; only the client's own deadline ends it.
        return new Promise((_resolve, reject) => {
          init.signal?.addEventListener("abort", () => {
            reject(Object.assign(new Error("aborted"), { name: "AbortError" }));
          });
        });
      });
      vi.useFakeTimers();

      // Assert before advancing, so the rejection is never momentarily unhandled.
      const settled = expect(createClient().getSandbox("sandbox-1")).rejects.toThrow(
        /timeout after 15000ms \(GET \/sandboxes\/sandbox-1\)/
      );
      await vi.advanceTimersByTimeAsync(15_000);

      await settled;
      vi.useRealTimers();
    });

    it("honours a caller's own cancellation", async () => {
      fetchSpy.mockImplementation(async (_url: string, init: RequestInit) => {
        return new Promise((_resolve, reject) => {
          init.signal?.addEventListener("abort", () => {
            reject(Object.assign(new Error("aborted"), { name: "AbortError" }));
          });
        });
      });
      const controller = new AbortController();

      const settled = expect(
        createClient().deleteSandbox("sandbox-1", controller.signal)
      ).rejects.toThrow(/timeout/);
      controller.abort();

      await settled;
    });
  });
});
