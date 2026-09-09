/**
 * macOS sandbox provider — sessions on an on-premise Mac, via its host agent.
 *
 * A thin translation layer, deliberately. Everything platform-specific lives
 * on the far side of the host agent's REST surface, so this file is REST
 * transport plus the shared environment contract and nothing else.
 *
 * Session continuity is provider-managed, the Daytona and E2B shape: stop
 * suspends and resume returns to the same sandbox, so there is no snapshot or
 * restore pair here. That is the design's choice rather than a gap — a
 * suspended VM releases its slot on the host (confirmed in CON-189), which
 * makes suspend the natural idle state, and it sidesteps the snapshot artifact
 * lifecycle entirely for v1.
 *
 * Two things are absent on purpose. There are no tunnel URLs: the interface
 * permits omitting them, and a public HTTPS endpoint per guest port needs
 * wildcard hostname routing that arrives with CON-179. And there is no
 * capacity special-casing: a host at its VM limit answers 503, which the
 * shared classifier already reads as transient, so a busy Mac degrades into a
 * retry instead of counting towards the spawn circuit breaker.
 */

import { createLogger } from "../../logger";
import type { SourceControlProviderName } from "../../source-control";
import {
  MacosHostAgentApiError,
  MacosHostAgentNotFoundError,
  type MacosHostAgentClient,
} from "../macos-host-agent-client";
import { buildSandboxEnvVars, deriveCodeServerPassword, scmCloneIdentity } from "../sandbox-env";
import {
  DEFAULT_SANDBOX_TIMEOUT_SECONDS,
  SandboxProviderError,
  type CreateSandboxConfig,
  type CreateSandboxResult,
  type ResumeConfig,
  type ResumeResult,
  type SandboxProvider,
  type SandboxProviderCapabilities,
  type StopConfig,
  type StopResult,
} from "../provider";

const log = createLogger("macos-provider");

export const DEFAULT_MACOS_SANDBOX_TIMEOUT_SECONDS = DEFAULT_SANDBOX_TIMEOUT_SECONDS;

export interface MacosProviderConfig {
  /** HMAC secret for browser-editor and desktop passwords. */
  sandboxAccessPasswordSecret: string;
  /** Sandbox lifetime when the session does not specify one. */
  sandboxTimeoutSeconds: number;
  scmProvider: SourceControlProviderName;
  /** Golden image the host agent clones per session. */
  goldenImage?: string;
}

export class MacosSandboxProvider implements SandboxProvider {
  readonly name = "macos";

  /**
   * Stop reasons after which the sandbox cannot be resumed and so must be torn
   * down rather than suspended. A session that never connected is marked
   * failed and will not be resumed, so suspending it would hold a VM slot --
   * of which this host has exactly two -- for nothing.
   */
  private static readonly TERMINAL_STOP_REASONS = new Set(["connecting_timeout", "respawn"]);

  readonly capabilities: SandboxProviderCapabilities = {
    supportsSandboxTimeout: true,
    supportsSnapshots: false,
    supportsRestore: false,
    supportsPersistentResume: true,
    supportsExplicitStop: true,
  };

  constructor(
    private readonly client: MacosHostAgentClient,
    private readonly providerConfig: MacosProviderConfig
  ) {}

  async createSandbox(config: CreateSandboxConfig): Promise<CreateSandboxResult> {
    try {
      const timeoutSeconds = config.timeoutSeconds ?? this.providerConfig.sandboxTimeoutSeconds;
      const codeServerPassword = config.codeServerEnabled
        ? await deriveCodeServerPassword(
            config.sandboxId,
            this.providerConfig.sandboxAccessPasswordSecret
          )
        : undefined;
      // No desktop password: the runtime's browser desktop shells out to an
      // X11 stack macOS does not have, so a Darwin sandbox disables it
      // outright. CON-180 replaces it with native Screen Sharing, and can
      // derive a credential when there is something to authenticate against.
      //
      // The rest is the shared assembly verbatim, because the runtime's
      // environment contract is non-trivial and shared by every backend --
      // forking it here would guarantee drift.
      const environment = buildSandboxEnvVars(config, {
        scmIdentity: scmCloneIdentity(this.providerConfig.scmProvider),
        codeServerPassword,
      });

      const sandbox = await this.client.createSandbox({
        sandboxId: config.sandboxId,
        // The session token rides the environment, which is the whole of the
        // secret path: the host agent hands it to the sandbox's process
        // environment and nothing reaches a command line.
        environment,
        timeoutSeconds,
        ...(this.providerConfig.goldenImage ? { image: this.providerConfig.goldenImage } : {}),
      });

      log.info("macos.sandbox_created", {
        session_id: config.sessionId,
        sandbox_id: sandbox.sandbox_id,
        status: sandbox.status,
      });

      return {
        sandboxId: config.sandboxId,
        providerObjectId: sandbox.sandbox_id,
        // The control plane's own clock, as every other provider does, so a
        // sandbox's age does not depend on the Mac agreeing about the time.
        createdAt: Date.now(),
        codeServerPassword,
      };
    } catch (error) {
      throw this.classifyError("Failed to create macOS sandbox", error);
    }
  }

  /**
   * Return to the sandbox a stop suspended. The session keeps its repository
   * checkout and its installed dependencies, which is the point of resuming
   * rather than spawning: a fresh macOS VM would have to clone and install
   * again.
   */
  async resumeSandbox(config: ResumeConfig): Promise<ResumeResult> {
    const timeoutSeconds = config.timeoutSeconds ?? this.providerConfig.sandboxTimeoutSeconds;
    try {
      let sandbox;
      try {
        sandbox = await this.client.resumeSandbox(config.providerObjectId);
      } catch (error) {
        // The host agent forgets a sandbox it has torn down, and a host that
        // was rebuilt has forgotten all of them. Either way there is nothing
        // to resume, so the manager should spawn fresh instead of failing.
        if (error instanceof MacosHostAgentNotFoundError) {
          return {
            success: false,
            error: "Sandbox no longer exists on the macOS host",
            shouldSpawnFresh: true,
          };
        }
        throw error;
      }

      if (sandbox.timeout_seconds !== timeoutSeconds) {
        await this.client.setTimeout(config.providerObjectId, timeoutSeconds);
      }

      const codeServerPassword = config.codeServerEnabled
        ? await deriveCodeServerPassword(
            config.sandboxId,
            this.providerConfig.sandboxAccessPasswordSecret
          )
        : undefined;

      log.info("macos.sandbox_resumed", {
        session_id: config.sessionId,
        sandbox_id: sandbox.sandbox_id,
      });

      return {
        success: true,
        providerObjectId: sandbox.sandbox_id,
        codeServerPassword,
      };
    } catch (error) {
      throw this.classifyError("Failed to resume macOS sandbox", error);
    }
  }

  /**
   * Idle and heartbeat stops suspend, so the next prompt resumes the same
   * sandbox. Terminal stops delete, because the session will never come back
   * and a VM slot is too scarce to hold for it.
   */
  async stopSandbox(config: StopConfig): Promise<StopResult> {
    const terminal = MacosSandboxProvider.TERMINAL_STOP_REASONS.has(config.reason);
    try {
      if (terminal) {
        await this.client.deleteSandbox(
          config.providerObjectId,
          ...(config.signal ? [config.signal] : [])
        );
      } else {
        await this.client.suspendSandbox(
          config.providerObjectId,
          ...(config.signal ? [config.signal] : [])
        );
      }
      return { success: true };
    } catch (error) {
      // Already gone is already stopped.
      if (error instanceof MacosHostAgentNotFoundError) {
        return { success: true };
      }
      throw this.classifyError(
        `Failed to stop (${terminal ? "delete" : "suspend"}) macOS sandbox`,
        error
      );
    }
  }

  private classifyError(message: string, error: unknown): SandboxProviderError {
    if (error instanceof MacosHostAgentApiError) {
      return SandboxProviderError.fromFetchError(
        `${message}: ${error.message}`,
        error,
        error.status
      );
    }
    return SandboxProviderError.fromFetchError(message, error);
  }
}

export function createMacosProvider(
  client: MacosHostAgentClient,
  config: MacosProviderConfig
): MacosSandboxProvider {
  return new MacosSandboxProvider(client, config);
}
