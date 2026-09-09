import { describe, expect, it } from "vitest";
import { createTestEnv } from "../router.test-support";
import type { Env } from "../types";
import { createSandboxProviderFromEnv } from "./provider-factory";

function createEnv(overrides: Partial<Env>): Env {
  return createTestEnv({ TOKEN_ENCRYPTION_KEY: "test-token-key", ...overrides });
}

describe("createSandboxProviderFromEnv", () => {
  it("rejects malformed Vercel numeric configuration", () => {
    const env = createEnv({
      VERCEL_TOKEN: "vercel-token",
      VERCEL_PROJECT_ID: "project-id",
      VERCEL_SNAPSHOT_EXPIRATION_MS: "10m",
    });

    expect(() => createSandboxProviderFromEnv(env, "vercel")).toThrow(
      "VERCEL_SNAPSHOT_EXPIRATION_MS must be a valid number"
    );
  });

  it("rejects malformed Daytona auto-stop configuration", () => {
    const env = createEnv({
      DAYTONA_API_URL: "https://daytona.test",
      DAYTONA_API_KEY: "daytona-key",
      DAYTONA_BASE_SNAPSHOT: "base",
      DAYTONA_AUTO_STOP_INTERVAL_MINUTES: "abc",
    });

    expect(() => createSandboxProviderFromEnv(env, "daytona")).toThrow(
      "DAYTONA_AUTO_STOP_INTERVAL_MINUTES must be a valid number"
    );
  });

  it("rejects malformed Daytona auto-archive configuration", () => {
    const env = createEnv({
      DAYTONA_API_URL: "https://daytona.test",
      DAYTONA_API_KEY: "daytona-key",
      DAYTONA_BASE_SNAPSHOT: "base",
      DAYTONA_AUTO_STOP_INTERVAL_MINUTES: "30",
      DAYTONA_AUTO_ARCHIVE_INTERVAL_MINUTES: "abc",
    });

    expect(() => createSandboxProviderFromEnv(env, "daytona")).toThrow(
      "DAYTONA_AUTO_ARCHIVE_INTERVAL_MINUTES must be a valid number"
    );
  });

  it("rejects malformed E2B auto-pause configuration", () => {
    const env = createEnv({
      E2B_API_KEY: "e2b-key",
      E2B_TEMPLATE_ID: "tmpl",
      E2B_AUTO_PAUSE: "tru",
    });

    expect(() => createSandboxProviderFromEnv(env, "e2b")).toThrow(
      "E2B_AUTO_PAUSE must be a valid boolean"
    );
  });

  describe("macOS host agent configuration", () => {
    const completeEnv = {
      MACOS_HOST_AGENT_URL: "https://mac-mini-5.example",
      MACOS_HOST_AGENT_API_KEY: "host-agent-key",
    };

    it("names both required variables when neither is set", () => {
      expect(() => createSandboxProviderFromEnv(createEnv({}), "macos")).toThrow(
        "MACOS_HOST_AGENT_URL and MACOS_HOST_AGENT_API_KEY are required when SANDBOX_PROVIDER=macos"
      );
    });

    it.each(["MACOS_HOST_AGENT_URL", "MACOS_HOST_AGENT_API_KEY"] as const)(
      "fails fast when %s is missing",
      (missing) => {
        const env = createEnv({ ...completeEnv, [missing]: undefined });

        expect(() => createSandboxProviderFromEnv(env, "macos")).toThrow(
          "required when SANDBOX_PROVIDER=macos"
        );
      }
    );

    it("rejects a malformed sandbox lifetime", () => {
      const env = createEnv({ ...completeEnv, MACOS_SANDBOX_TIMEOUT_SECONDS: "two hours" });

      expect(() => createSandboxProviderFromEnv(env, "macos")).toThrow(
        "MACOS_SANDBOX_TIMEOUT_SECONDS must be a valid number"
      );
    });

    it("builds the provider from a complete environment", () => {
      const provider = createSandboxProviderFromEnv(createEnv(completeEnv), "macos");

      expect(provider.name).toBe("macos");
    });

    it("is what SANDBOX_PROVIDER=macos selects", () => {
      const env = createEnv({ ...completeEnv, SANDBOX_PROVIDER: "macos" });

      expect(createSandboxProviderFromEnv(env).name).toBe("macos");
    });
  });

  it("requires an OpenComputer template for starts but not existing-session cleanup", () => {
    const env = createEnv({
      OPENCOMPUTER_API_URL: "https://opencomputer.test",
      OPENCOMPUTER_API_KEY: "opencomputer-key",
    });

    expect(() => createSandboxProviderFromEnv(env, "opencomputer")).toThrow(
      "OPENCOMPUTER_TEMPLATE"
    );
    expect(() =>
      createSandboxProviderFromEnv(env, "opencomputer", {
        requireOpenComputerTemplate: false,
      })
    ).not.toThrow();
  });
});
