from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

import pytest
from fastapi.testclient import TestClient

from macos_host_agent.app import API_KEY_HEADER, create_app
from macos_host_agent.config import HostAgentConfig
from macos_host_agent.driver import DriverHandle, ExecResult, SandboxSpec

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

API_KEY = "k" * 32


class FakeDriver:
    """A driver that records what it was asked to do and runs nothing.

    The seam exists so the service can be tested on any machine, so this is the
    reason the seam exists rather than a convenience.
    """

    name: ClassVar[str] = "fake"

    def __init__(self) -> None:
        self.created: list[SandboxSpec] = []
        self.resumed: list[SandboxSpec] = []
        self.suspended: list[str] = []
        self.deleted: list[str] = []
        self.executed: list[tuple[str, tuple[str, ...], Mapping[str, str] | None]] = []
        self.running: set[str] = set()
        self.exec_result = ExecResult(exit_code=0, stdout="", stderr="")
        self.fail_create_with: Exception | None = None
        self.fail_resume_with: Exception | None = None

    async def create(self, spec: SandboxSpec) -> DriverHandle:
        if self.fail_create_with is not None:
            raise self.fail_create_with
        self.created.append(spec)
        self.running.add(spec.sandbox_id)
        return DriverHandle(driver_id=f"fake-{spec.sandbox_id}")

    async def is_running(self, handle: DriverHandle) -> bool:
        return handle.driver_id.removeprefix("fake-") in self.running

    async def execute(
        self,
        handle: DriverHandle,
        argv: Sequence[str],
        *,
        environment: Mapping[str, str] | None = None,
    ) -> ExecResult:
        self.executed.append((handle.driver_id, tuple(argv), environment))
        return self.exec_result

    async def suspend(self, handle: DriverHandle) -> None:
        self.suspended.append(handle.driver_id)
        self.running.discard(handle.driver_id.removeprefix("fake-"))

    async def resume(self, handle: DriverHandle, spec: SandboxSpec) -> DriverHandle:
        if self.fail_resume_with is not None:
            raise self.fail_resume_with
        self.resumed.append(spec)
        self.running.add(spec.sandbox_id)
        return handle

    async def delete(self, handle: DriverHandle) -> None:
        self.deleted.append(handle.driver_id)
        self.running.discard(handle.driver_id.removeprefix("fake-"))

    async def running_ids(self) -> frozenset[str]:
        return frozenset(self.running)


@pytest.fixture
def config(tmp_path: Path) -> HostAgentConfig:
    return HostAgentConfig(
        api_key=API_KEY,
        sandbox_root=tmp_path / "sandboxes",
        runtime_python=tmp_path / "python",
        sandbox_path="/usr/bin:/bin",
        host="127.0.0.1",
        port=8900,
    )


@pytest.fixture
def driver() -> FakeDriver:
    return FakeDriver()


@pytest.fixture
def client(config: HostAgentConfig, driver: FakeDriver) -> TestClient:
    """A client that authenticates. Tests about auth build their own."""
    return TestClient(create_app(config, driver), headers={API_KEY_HEADER: config.api_key})
