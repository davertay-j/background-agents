import { describe, expect, it, vi } from "vitest";
import { MacosSandboxProvider } from "./macos-provider";
import type { MacosHostAgentClient, MacosSandbox } from "../macos-host-agent-client";
import { MacosHostAgentApiError, MacosHostAgentNotFoundError } from "../macos-host-agent-client";
import type { CreateSandboxConfig, ResumeConfig, SandboxProvider, StopConfig } from "../provider";
import { SandboxProviderError } from "../provider";

function sandbox(overrides: Partial<MacosSandbox> = {}): MacosSandbox {
  return {
    sandbox_id: "sandbox-acme-repo-1",
    status: "running",
    created_at_ms: 1_700_000_000_000,
    expires_at_ms: 1_700_000_007_200_000,
    timeout_seconds: 7200,
    ...overrides,
  };
}

function createMockClient(overrides: Partial<MacosHostAgentClient> = {}): MacosHostAgentClient {
  const client = {
    config: { apiUrl: "https://mac-mini-5.example", apiKey: "host-agent-key" },
    createSandbox: vi.fn(async (): Promise<MacosSandbox> => sandbox()),
    getSandbox: vi.fn(async (): Promise<MacosSandbox> => sandbox()),
    suspendSandbox: vi.fn(async (): Promise<MacosSandbox> => sandbox({ status: "suspended" })),
    resumeSandbox: vi.fn(async (): Promise<MacosSandbox> => sandbox()),
    setTimeout: vi.fn(async (): Promise<MacosSandbox> => sandbox()),
    execute: vi.fn(async () => ({ exit_code: 0, stdout: "", stderr: "" })),
    deleteSandbox: vi.fn(async (): Promise<void> => undefined),
    ...overrides,
  };
  return client as unknown as MacosHostAgentClient;
}

function createProvider(client: MacosHostAgentClient = createMockClient()) {
  return new MacosSandboxProvider(client, {
    scmProvider: "github",
    sandboxAccessPasswordSecret: "host-agent-key",
    sandboxTimeoutSeconds: 7200,
  });
}

const baseConfig: CreateSandboxConfig = {
  sessionId: "session-1",
  sandboxId: "sandbox-acme-repo-1",
  repoOwner: "acme",
  repoName: "repo",
  controlPlaneUrl: "https://control.example",
  sandboxAuthToken: "sandbox-token",
  provider: "anthropic",
  model: "claude-sonnet-4-6",
  branch: "main",
};

const resumeConfig: ResumeConfig = {
  providerObjectId: "sandbox-acme-repo-1",
  sessionId: "session-1",
  sandboxId: "sandbox-acme-repo-1",
  timeoutSeconds: 7200,
};

function stopConfig(reason: string): StopConfig {
  return { providerObjectId: "sandbox-acme-repo-1", sessionId: "session-1", reason };
}

describe("MacosSandboxProvider", () => {
  it("resumes in place rather than snapshotting", () => {
    const provider = createProvider();

    expect(provider.name).toBe("macos");
    expect(provider.capabilities).toEqual({
      supportsSandboxTimeout: true,
      supportsSnapshots: false,
      supportsRestore: false,
      supportsPersistentResume: true,
      supportsExplicitStop: true,
    });
  });

  it("declares no snapshot or restore methods at all", () => {
    // The lifecycle manager gates on method presence, not only the capability
    // flags, so absent is the honest way to say unsupported. Widened to the
    // interface deliberately: that is how the manager holds a provider, and on
    // the concrete class these properties do not exist to be read at all.
    const provider: SandboxProvider = createProvider();

    expect(provider.takeSnapshot).toBeUndefined();
    expect(provider.restoreFromSnapshot).toBeUndefined();
  });

  describe("createSandbox", () => {
    it("assembles the shared sandbox environment", async () => {
      const client = createMockClient();

      const result = await createProvider(client).createSandbox({
        ...baseConfig,
        userEnvVars: { ANTHROPIC_API_KEY: "sk-test" },
      });

      expect(result).toMatchObject({
        sandboxId: "sandbox-acme-repo-1",
        providerObjectId: "sandbox-acme-repo-1",
      });
      expect(client.createSandbox).toHaveBeenCalledWith(
        expect.objectContaining({
          sandboxId: "sandbox-acme-repo-1",
          timeoutSeconds: 7200,
          environment: expect.objectContaining({
            SANDBOX_ID: "sandbox-acme-repo-1",
            CONTROL_PLANE_URL: "https://control.example",
            SANDBOX_AUTH_TOKEN: "sandbox-token",
            REPO_OWNER: "acme",
            REPO_NAME: "repo",
            VCS_HOST: "github.com",
            VCS_CLONE_USERNAME: "x-access-token",
            ANTHROPIC_API_KEY: "sk-test",
          }),
        })
      );
    });

    it("carries the session token in the environment and nowhere else", async () => {
      const client = createMockClient();

      await createProvider(client).createSandbox(baseConfig);

      const [request] = vi.mocked(client.createSandbox).mock.calls[0];
      expect(request.environment.SANDBOX_AUTH_TOKEN).toBe("sandbox-token");
      // Nothing outside `environment` may carry it — the host agent hands that
      // map to the sandbox's process environment, and everything else it
      // receives could end up on a command line.
      const { environment: _environment, ...rest } = request;
      expect(JSON.stringify(rest)).not.toContain("sandbox-token");
    });

    it("honours the session's own timeout over the configured default", async () => {
      const client = createMockClient();

      await createProvider(client).createSandbox({ ...baseConfig, timeoutSeconds: 900 });

      expect(client.createSandbox).toHaveBeenCalledWith(
        expect.objectContaining({ timeoutSeconds: 900 })
      );
    });

    it("names the golden image when the deployment configures one", async () => {
      const client = createMockClient();
      const provider = new MacosSandboxProvider(client, {
        scmProvider: "github",
        sandboxAccessPasswordSecret: "host-agent-key",
        sandboxTimeoutSeconds: 7200,
        goldenImage: "openinspect/macos-xcode:26",
      });

      await provider.createSandbox(baseConfig);

      expect(client.createSandbox).toHaveBeenCalledWith(
        expect.objectContaining({ image: "openinspect/macos-xcode:26" })
      );
    });

    it("derives a browser-editor password only when the editor is enabled", async () => {
      const client = createMockClient();
      const provider = createProvider(client);

      const withEditor = await provider.createSandbox({ ...baseConfig, codeServerEnabled: true });
      const withoutEditor = await provider.createSandbox(baseConfig);

      expect(withEditor.codeServerPassword).toMatch(/^[0-9a-f]{32}$/);
      expect(withoutEditor.codeServerPassword).toBeUndefined();
    });

    it("omits tunnel URLs, which this slice does not provide", async () => {
      // The interface permits omitting them; wildcard hostname routing lands
      // with CON-179.
      const result = await createProvider().createSandbox({
        ...baseConfig,
        codeServerEnabled: true,
      });

      expect(result.codeServerUrl).toBeUndefined();
      expect(result.ttydUrl).toBeUndefined();
      expect(result.tunnelUrls).toBeUndefined();
    });

    it("derives no desktop password, because a Darwin sandbox has no desktop", async () => {
      // The runtime's browser desktop needs an X11 stack macOS lacks, so it
      // disables itself there. CON-180 replaces it with Screen Sharing.
      const client = createMockClient();

      const result = await createProvider(client).createSandbox({
        ...baseConfig,
        vncEnabled: true,
      });

      expect(result.vncAccess).toBeUndefined();
      const [request] = vi.mocked(client.createSandbox).mock.calls[0];
      expect(request.environment).not.toHaveProperty("VNC_PASSWORD");
    });
  });

  describe("resumeSandbox", () => {
    it("returns to the same sandbox", async () => {
      const client = createMockClient();

      const result = await createProvider(client).resumeSandbox(resumeConfig);

      expect(result).toMatchObject({ success: true, providerObjectId: "sandbox-acme-repo-1" });
      expect(client.resumeSandbox).toHaveBeenCalledWith("sandbox-acme-repo-1");
      expect(client.createSandbox).not.toHaveBeenCalled();
    });

    it("re-bases the lifetime when the host's deadline no longer matches policy", async () => {
      const client = createMockClient({
        resumeSandbox: vi.fn(async () => sandbox({ timeout_seconds: 600 })),
      });

      await createProvider(client).resumeSandbox(resumeConfig);

      expect(client.setTimeout).toHaveBeenCalledWith("sandbox-acme-repo-1", 7200);
    });

    it("leaves a matching lifetime alone", async () => {
      const client = createMockClient();

      await createProvider(client).resumeSandbox(resumeConfig);

      expect(client.setTimeout).not.toHaveBeenCalled();
    });

    it("asks for a fresh spawn when the host has forgotten the sandbox", async () => {
      // A rebuilt or restarted host has forgotten all of them, and failing the
      // session would be worse than starting again.
      const client = createMockClient({
        resumeSandbox: vi.fn(async () => {
          throw new MacosHostAgentNotFoundError("No sandbox sandbox-acme-repo-1");
        }),
      });

      const result = await createProvider(client).resumeSandbox(resumeConfig);

      expect(result).toMatchObject({ success: false, shouldSpawnFresh: true });
    });

    it("classifies a busy host as transient so the breaker does not trip", async () => {
      const client = createMockClient({
        resumeSandbox: vi.fn(async () => {
          throw new MacosHostAgentApiError("Both VM slots are in use", 503);
        }),
      });

      await expect(createProvider(client).resumeSandbox(resumeConfig)).rejects.toMatchObject({
        errorType: "transient",
      });
    });
  });

  describe("stopSandbox", () => {
    it("suspends an idle session so its next prompt resumes it", async () => {
      const client = createMockClient();

      const result = await createProvider(client).stopSandbox(stopConfig("inactivity_timeout"));

      expect(result).toEqual({ success: true });
      expect(client.suspendSandbox).toHaveBeenCalledWith("sandbox-acme-repo-1");
      expect(client.deleteSandbox).not.toHaveBeenCalled();
    });

    it("suspends on a stale heartbeat too", async () => {
      const client = createMockClient();

      await createProvider(client).stopSandbox(stopConfig("heartbeat_timeout"));

      expect(client.suspendSandbox).toHaveBeenCalled();
    });

    it.each(["connecting_timeout", "respawn"])(
      "deletes rather than suspends after a %s stop",
      async (reason) => {
        // The session will not be resumed, and this host has exactly two VM
        // slots to hold.
        const client = createMockClient();

        await createProvider(client).stopSandbox(stopConfig(reason));

        expect(client.deleteSandbox).toHaveBeenCalledWith("sandbox-acme-repo-1");
        expect(client.suspendSandbox).not.toHaveBeenCalled();
      }
    );

    it("treats an already-gone sandbox as already stopped", async () => {
      const client = createMockClient({
        suspendSandbox: vi.fn(async () => {
          throw new MacosHostAgentNotFoundError("No sandbox sandbox-acme-repo-1");
        }),
      });

      const result = await createProvider(client).stopSandbox(stopConfig("inactivity_timeout"));

      expect(result).toEqual({ success: true });
    });
  });

  describe("error classification", () => {
    it.each([
      [503, "transient"],
      [502, "transient"],
      [504, "transient"],
      [401, "permanent"],
      [409, "permanent"],
      [500, "permanent"],
    ])("reports an HTTP %i from the host agent as %s", async (status, errorType) => {
      const client = createMockClient({
        createSandbox: vi.fn(async () => {
          throw new MacosHostAgentApiError("host agent said no", status as number);
        }),
      });

      await expect(createProvider(client).createSandbox(baseConfig)).rejects.toMatchObject({
        errorType,
      });
    });

    it("reports an unreachable host as transient", async () => {
      const client = createMockClient({
        createSandbox: vi.fn(async () => {
          throw new Error("fetch failed");
        }),
      });

      const error = await createProvider(client)
        .createSandbox(baseConfig)
        .catch((raised: unknown) => raised);

      expect(error).toBeInstanceOf(SandboxProviderError);
      expect(error).toMatchObject({ errorType: "transient" });
    });

    it("reports a request that outran its deadline as transient", async () => {
      const client = createMockClient({
        createSandbox: vi.fn(async () => {
          throw new Error("macOS host agent request timeout after 180000ms (POST /sandboxes)");
        }),
      });

      await expect(createProvider(client).createSandbox(baseConfig)).rejects.toMatchObject({
        errorType: "transient",
      });
    });
  });
});
