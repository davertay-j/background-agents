"""The seam between the host agent's HTTP surface and how a sandbox actually runs.

The service knows sandboxes by an opaque `driver_id` and asks the driver
whether one is alive; it never holds a process handle. That is not indirection
for its own sake -- `tart suspend` terminates the `tart run` process, so a
suspended VM is very much alive while the handle that started it is gone. A
service that equated "my child process exited" with "the sandbox is gone"
would be correct for a local process and wrong for every VM.

Suspend and resume are the load-bearing pair, because the control-plane
provider declares persistent resume and explicit stop rather than snapshots.
What has to survive a suspend is the sandbox's *state* -- the repository
checkout, the installed dependencies -- not the process that was serving it.
Both drivers honour that, by different means: Tart keeps the VM's disk and
memory, and `LocalProcessDriver` keeps the scratch directory and starts a new
process against it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Protocol

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


@dataclass(frozen=True)
class SandboxSpec:
    """Everything a driver needs to bring one sandbox up.

    `environment` is the sandbox environment the control plane assembled, which
    carries the session's auth token. It reaches the sandbox as an environment
    and never as command arguments.
    """

    sandbox_id: str
    environment: Mapping[str, str]
    timeout_seconds: int
    image: str | None = None
    """Golden image to clone. Ignored by drivers that do not virtualize."""


@dataclass(frozen=True)
class DriverHandle:
    """A driver's own name for a sandbox, meaningless to the service."""

    driver_id: str


@dataclass(frozen=True)
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str


class DriverError(Exception):
    """The driver could not carry out the operation."""


class CapacityExhausted(DriverError):
    """The host cannot run another sandbox right now.

    Separate from every other failure because it is routine rather than broken:
    the service answers it with a status the control plane already classifies as
    transient, so a busy host does not trip the spawn circuit breaker.
    """


class SandboxDriver(Protocol):
    """How one kind of host runs sandboxes."""

    name: ClassVar[str]

    async def create(self, spec: SandboxSpec) -> DriverHandle:
        """Bring up a sandbox and return the handle it will be known by."""
        ...

    async def is_running(self, handle: DriverHandle) -> bool:
        """Whether this sandbox is currently executing.

        Asked rather than remembered, so a suspend that terminates the process
        the driver started does not read as a vanished sandbox.
        """
        ...

    async def execute(
        self,
        handle: DriverHandle,
        argv: Sequence[str],
        *,
        environment: Mapping[str, str] | None = None,
    ) -> ExecResult:
        """Run a command inside the sandbox and wait for it."""
        ...

    async def suspend(self, handle: DriverHandle) -> None:
        """Stop executing, keeping enough state to resume in place."""
        ...

    async def resume(self, handle: DriverHandle, spec: SandboxSpec) -> DriverHandle:
        """Start executing again against the state a suspend preserved.

        Takes the spec back because a driver that cannot literally freeze a
        process has to relaunch one, and relaunching needs the environment. A
        driver that resumes a real VM ignores it.
        """
        ...

    async def delete(self, handle: DriverHandle) -> None:
        """Tear the sandbox down and release everything it holds."""
        ...

    async def running_ids(self) -> frozenset[str]:
        """Every sandbox this driver currently observes running.

        The startup reconciliation in CON-178 compares this against the
        service's own records; nothing else should need it.
        """
        ...
