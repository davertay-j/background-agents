# macOS host agent

An HTTP service that runs Open-Inspect sandboxes on an on-premise Mac. The control plane's `macos`
sandbox provider is a thin translation layer over the surface described below.

The Mac exists so Apple platform work can run in a background session at all: no Linux container can
build, test, or run an iOS or macOS target, which excludes those repositories from every other
backend.

## Where the platform-specific part lives

Everything about _how_ a sandbox runs sits behind the `SandboxDriver` seam in `driver.py`. Two
drivers are planned and only one exists:

| Driver               | Status  | What a sandbox is                            |
| -------------------- | ------- | -------------------------------------------- |
| `LocalProcessDriver` | Shipped | A process in a scratch directory on the host |
| Tart driver          | CON-172 | A macOS VM cloned from a golden image        |

The local-process driver takes virtualization out of the first slice. The provider, this service,
the sandbox environment contract, the bridge, and event streaming are all the same code the Tart
driver will run under, so proving the chain now leaves Tart as the only remaining unknown rather
than one of two.

It provides **no isolation**: sandboxes share the user, the filesystem, and the network, and get
only their own scratch directory. That is acceptable on a single-tenant on-premise host during a
spike, and it is why this driver must not be the one running real sessions.

## Running it

```bash
uv sync --extra dev
OI_HOST_AGENT_API_KEY=... \
OI_HOST_AGENT_SANDBOX_ROOT=/var/openinspect/sandboxes \
OI_HOST_AGENT_RUNTIME_PYTHON=/opt/openinspect/python/bin/python \
  uv run python -m macos_host_agent.main
```

| Variable                       | Required | Meaning                                        |
| ------------------------------ | -------- | ---------------------------------------------- |
| `OI_HOST_AGENT_API_KEY`        | yes      | Pre-shared key, 32+ characters                 |
| `OI_HOST_AGENT_SANDBOX_ROOT`   | yes      | Parent of the per-sandbox scratch directories  |
| `OI_HOST_AGENT_RUNTIME_PYTHON` | yes      | Interpreter with the sandbox runtime installed |
| `OI_HOST_AGENT_SANDBOX_PATH`   | no       | `PATH` for sandbox processes                   |
| `OI_HOST_AGENT_HOST`           | no       | Listen address, default `127.0.0.1`            |
| `OI_HOST_AGENT_PORT`           | no       | Listen port, default `8900`                    |

A host that cannot be configured exits naming every missing variable at once, rather than starting
and failing the first request.

`OI_HOST_AGENT_API_KEY` is not only a network guard: the control plane derives the browser-editor
and desktop passwords from it by HMAC, so it is a real secret and must match
`MACOS_HOST_AGENT_API_KEY` on the control plane.

The default `PATH` names Homebrew's keg-only `node@22` bin directory explicitly. A repository's
setup hook typically checks for node before doing anything, and a hook that cannot find it fails the
whole install step.

## API

Every route below requires `X-OI-Host-Agent-Key`. Unauthenticated requests are rejected before any
work happens — a sandbox holds live repository credentials, so an unauthenticated create is a way to
be handed a token, not merely an unwanted process. `GET /healthz` is deliberately open, so a
supervisor or tunnel can probe liveness without holding the key; it reports nothing about any
session.

| Route                          | Purpose                                       |
| ------------------------------ | --------------------------------------------- |
| `POST /sandboxes`              | Create and start a sandbox                    |
| `GET /sandboxes/{id}`          | Current status and deadline                   |
| `POST /sandboxes/{id}/exec`    | Run a command inside the sandbox              |
| `POST /sandboxes/{id}/suspend` | Stop executing, keeping state                 |
| `POST /sandboxes/{id}/resume`  | Execute again against the state suspend kept  |
| `POST /sandboxes/{id}/timeout` | Re-base the sandbox's lifetime                |
| `DELETE /sandboxes/{id}`       | Tear down and release everything (idempotent) |

### Status codes are part of the contract

The control plane classifies a provider failure as transient or permanent and trips a circuit
breaker on repeated permanent ones, deriving that from the HTTP status. So:

- **503** — the host cannot run another sandbox right now. Routine and retryable; already transient
  in the control plane's shared classifier, so a busy Mac degrades into a retry instead of blocking
  sessions.
- **500** — the host is genuinely broken, and is allowed to trip the breaker.
- **409** — a live sandbox already holds that id.
- **404** — no such sandbox. The provider reads this as "spawn fresh instead".

### Suspend and resume

The provider declares persistent resume rather than snapshots, so what has to survive a suspend is
the sandbox's _state_ — the repository checkout, the installed dependencies — and not the process
serving it. Both drivers honour that by different means: Tart keeps the VM's disk and memory, and
`LocalProcessDriver` keeps the scratch directory and starts a new process against it.

Suspending also matters for capacity. Apple's SLA permits exactly two virtualised macOS instances
per host, kernel-enforced, and CON-189 confirmed that suspending a VM releases its slot. Idle
sessions therefore cannot starve the host.

## Secrets

The session's auth token and every repository secret reach a sandbox through its process
environment, delivered in the `POST /sandboxes` body. Nothing sensitive is ever passed as a command
argument, where any local `ps` would read it. `tests/test_local_process_driver.py` asserts this
against the command line the operating system actually reports.

## Tests

```bash
uv run pytest tests/ -v
```

No test requires Apple hardware or boots a VM. The service is tested against a fake driver, and the
local-process driver is tested with real processes and a fake interpreter — so the assertions that
need a real launch (what `ps` shows, that a process group dies as one, that a suspend leaves the
workspace behind) are still made against one.

## Not yet built

| Concern                            | Ticket  |
| ---------------------------------- | ------- |
| Tart driver, real VMs              | CON-172 |
| Capacity semaphore and queueing    | CON-174 |
| Suspending idle sandboxes here     | CON-177 |
| Reconciling with Tart on startup   | CON-178 |
| Tunnel URLs for editor/desktop/tty | CON-179 |

`driver.running_ids()` exists for the reconciliation in CON-178 and has no other caller yet.
