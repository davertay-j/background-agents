"""A driver that runs the sandbox runtime as a plain process on this machine.

This is the tracer bullet's driver, and it exists to take virtualization out of
the first slice. Everything above it -- the provider, the host agent's HTTP
surface, the sandbox environment contract, the bridge, event streaming -- is
the same code the Tart driver will run under, so proving the chain here leaves
Tart as the only remaining unknown rather than one of two.

What it deliberately does not provide is isolation. Sandboxes get their own
scratch directory and nothing more: same user, same filesystem, same network.
That is acceptable for a single-tenant on-premise host during a spike, and it
is the reason this driver must not be the one running real sessions.

Suspend terminates the sandbox's process tree and keeps its scratch directory;
resume starts a new process against that directory. So a suspended sandbox
keeps its repository checkout and its installed dependencies, and loses the
agent's in-flight process state -- which is the same bargain the control plane
already makes with providers that cannot snapshot memory.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
from typing import TYPE_CHECKING, ClassVar

from .driver import DriverError, DriverHandle, ExecResult, SandboxSpec

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

RUNTIME_MODULE = "sandbox_runtime.entrypoint"
"""How long a terminated sandbox gets to exit before it is killed."""
TERMINATE_GRACE_SECONDS = 5.0
"""Cap on captured `exec` output, so one command cannot exhaust memory here."""
MAX_CAPTURED_OUTPUT_BYTES = 1024 * 1024


class LocalProcessDriver:
    """Runs each sandbox as a detached process in its own scratch directory."""

    name: ClassVar[str] = "local-process"

    def __init__(
        self,
        *,
        sandbox_root: Path,
        runtime_python: Path,
        sandbox_path: str,
    ) -> None:
        self._sandbox_root = sandbox_root
        self._runtime_python = runtime_python
        self._sandbox_path = sandbox_path
        self._processes: dict[str, asyncio.subprocess.Process] = {}

    async def create(self, spec: SandboxSpec) -> DriverHandle:
        directory = self._directory(spec.sandbox_id)
        # A create for an id we have seen before starts from nothing, so a
        # replacement sandbox cannot inherit the previous one's workspace.
        await asyncio.to_thread(shutil.rmtree, directory, True)
        handle = DriverHandle(driver_id=spec.sandbox_id)
        await self._launch(handle, spec)
        return handle

    async def is_running(self, handle: DriverHandle) -> bool:
        process = self._processes.get(handle.driver_id)
        return process is not None and process.returncode is None

    async def execute(
        self,
        handle: DriverHandle,
        argv: Sequence[str],
        *,
        environment: Mapping[str, str] | None = None,
    ) -> ExecResult:
        directory = self._directory(handle.driver_id)
        if not directory.is_dir():
            raise DriverError(f"No sandbox directory for {handle.driver_id}")
        # An exec should see what the sandbox sees: its own home rather than the
        # operator's, and its own bin directory ahead of the host's.
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=directory,
            env={
                "HOME": str(directory / "home"),
                "TMPDIR": str(directory / "tmp"),
                "PATH": f"{directory / 'bin'}:{self._sandbox_path}",
                **(environment or {}),
            },
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        stdout, stderr = await process.communicate()
        return ExecResult(
            exit_code=process.returncode if process.returncode is not None else -1,
            stdout=_decode(stdout),
            stderr=_decode(stderr),
        )

    async def suspend(self, handle: DriverHandle) -> None:
        await self._terminate(handle)

    async def resume(self, handle: DriverHandle, spec: SandboxSpec) -> DriverHandle:
        if not self._directory(handle.driver_id).is_dir():
            raise DriverError(
                f"Cannot resume {handle.driver_id}: its workspace is gone. "
                "The sandbox has to be created again."
            )
        await self._launch(handle, spec)
        return handle

    async def delete(self, handle: DriverHandle) -> None:
        await self._terminate(handle)
        self._processes.pop(handle.driver_id, None)
        await asyncio.to_thread(shutil.rmtree, self._directory(handle.driver_id), True)

    async def running_ids(self) -> frozenset[str]:
        return frozenset(
            driver_id
            for driver_id, process in self._processes.items()
            if process.returncode is None
        )

    def _directory(self, driver_id: str) -> Path:
        return self._sandbox_root / driver_id

    async def _launch(self, handle: DriverHandle, spec: SandboxSpec) -> None:
        directory = self._directory(handle.driver_id)
        workspace = directory / "workspace"
        bin_dir = directory / "bin"
        home = directory / "home"
        tmp = directory / "tmp"
        for path in (workspace, bin_dir, home, tmp, directory / "config"):
            await asyncio.to_thread(path.mkdir, 0o700, True, True)

        log_path = directory / "runtime.log"
        log = await asyncio.to_thread(log_path.open, "ab")
        try:
            process = await asyncio.create_subprocess_exec(
                str(self._runtime_python),
                "-m",
                RUNTIME_MODULE,
                cwd=directory,
                env=self._environment(
                    spec, workspace=workspace, bin_dir=bin_dir, home=home, tmp=tmp
                ),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=log,
                stderr=asyncio.subprocess.STDOUT,
                # Its own process group, so terminating the sandbox takes the
                # agent and every tool it started rather than just the supervisor.
                start_new_session=True,
            )
        except OSError as error:
            raise DriverError(f"Could not start the sandbox runtime: {error}") from error
        finally:
            log.close()
        self._processes[handle.driver_id] = process

    def _environment(
        self, spec: SandboxSpec, *, workspace: Path, bin_dir: Path, home: Path, tmp: Path
    ) -> dict[str, str]:
        """The sandbox's environment: the control plane's, plus what only the host knows.

        The runtime resolves its own platform defaults, but it never creates
        directories -- an image or a launcher owns the layout. This driver is
        the launcher, so it names the locations it just made.

        `HOME` is one of those locations, and it must not be inherited. The
        runtime writes into the home directory it is given -- a git credential
        helper, agent configuration, tool caches -- and this driver shares a
        user account with whoever operates the host. Inheriting `HOME` therefore
        lets a sandbox rewrite the operator's own dotfiles, and it does: an
        earlier run of the runtime under a real `HOME` left a credential helper
        in a developer's global git config pointing into a scratch directory
        that no longer existed, which breaks every later authenticated fetch.
        """
        environment = {
            "HOME": str(home),
            "TMPDIR": str(tmp),
            # Ahead of every other entry, so the runtime's authenticated `gh`
            # wrapper shadows the real one the way it does in the Linux image.
            "PATH": f"{bin_dir}:{self._sandbox_path}",
            **dict(spec.environment),
            "OI_WORKSPACE_ROOT": str(workspace),
            "OPENINSPECT_BIN_INSTALL_DIR": str(bin_dir),
            "SANDBOX_ID": spec.sandbox_id,
            "SANDBOX_TIMEOUT_SECONDS": str(spec.timeout_seconds),
        }
        if spec.environment.get("PATH"):
            environment["PATH"] = f"{bin_dir}:{spec.environment['PATH']}"
        return environment

    async def _terminate(self, handle: DriverHandle) -> None:
        process = self._processes.get(handle.driver_id)
        if process is None or process.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), timeout=TERMINATE_GRACE_SECONDS)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            await process.wait()


def _decode(raw: bytes) -> str:
    return raw[:MAX_CAPTURED_OUTPUT_BYTES].decode("utf-8", errors="replace")
