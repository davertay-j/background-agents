"""The local-process driver, exercised with real processes.

The interpreter is faked rather than the subprocess machinery, so these tests
still prove the things that only a real launch can: what the OS sees on the
command line, that a process group dies as one, and that a suspend leaves the
workspace behind.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from macos_host_agent.driver import DriverError, SandboxSpec
from macos_host_agent.local_process_driver import LocalProcessDriver

if TYPE_CHECKING:
    from pathlib import Path

SESSION_TOKEN = "sandbox-token-1234567890abcdef"


@pytest.fixture
def fake_interpreter(tmp_path: Path) -> Path:
    """Stands in for the image's Python.

    It records its own arguments, its environment, and its command line as the
    operating system reports it, then idles so the driver has something live to
    suspend.
    """
    interpreter = tmp_path / "fake-python"
    interpreter.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$@" > argv.txt\n'
        "env > env.txt\n"
        "ps -o args= -p $$ > cmdline.txt 2>/dev/null || true\n"
        "exec sleep 300\n"
    )
    interpreter.chmod(0o755)
    return interpreter


@pytest.fixture
def driver(tmp_path: Path, fake_interpreter: Path) -> LocalProcessDriver:
    return LocalProcessDriver(
        sandbox_root=tmp_path / "sandboxes",
        runtime_python=fake_interpreter,
        sandbox_path="/usr/bin:/bin",
    )


def spec(**overrides: object) -> SandboxSpec:
    base: dict[str, object] = {
        "sandbox_id": "sandbox-1",
        "environment": {"SANDBOX_AUTH_TOKEN": SESSION_TOKEN, "REPO_NAME": "storefront"},
        "timeout_seconds": 3600,
    }
    base.update(overrides)
    return SandboxSpec(**base)  # type: ignore[arg-type]


async def wait_for(path: Path, timeout_seconds: float = 5.0) -> str:
    """The sandbox writes these as it starts, so give it a moment."""
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        if path.is_file() and path.stat().st_size > 0:
            return path.read_text()
        await asyncio.sleep(0.02)
    raise AssertionError(f"{path} never appeared")


class TestCreate:
    async def test_launches_the_runtime_and_reports_it_running(
        self, driver: LocalProcessDriver
    ) -> None:
        handle = await driver.create(spec())
        try:
            argv = await wait_for(driver._directory("sandbox-1") / "argv.txt")

            assert argv.split() == ["-m", "sandbox_runtime.entrypoint"]
            assert await driver.is_running(handle) is True
        finally:
            await driver.delete(handle)

    async def test_starts_from_an_empty_workspace(self, driver: LocalProcessDriver) -> None:
        """A replacement sandbox must not inherit the previous one's checkout."""
        handle = await driver.create(spec())
        await wait_for(driver._directory("sandbox-1") / "argv.txt")
        stale = driver._directory("sandbox-1") / "workspace" / "left-behind.txt"
        stale.write_text("from an earlier sandbox")
        await driver.suspend(handle)

        await driver.create(spec())
        try:
            assert stale.exists() is False
        finally:
            await driver.delete(handle)

    async def test_reports_an_interpreter_that_cannot_be_started(self, tmp_path: Path) -> None:
        driver = LocalProcessDriver(
            sandbox_root=tmp_path / "sandboxes",
            runtime_python=tmp_path / "does-not-exist",
            sandbox_path="/usr/bin:/bin",
        )

        with pytest.raises(DriverError, match="Could not start the sandbox runtime"):
            await driver.create(spec())


class TestSecretDelivery:
    async def test_the_session_token_reaches_the_sandbox_by_environment_only(
        self, driver: LocalProcessDriver
    ) -> None:
        """Not on the command line, where anyone with `ps` could read it."""
        handle = await driver.create(spec())
        try:
            directory = driver._directory("sandbox-1")
            environment = await wait_for(directory / "env.txt")
            argv = await wait_for(directory / "argv.txt")
            command_line = await wait_for(directory / "cmdline.txt")

            assert f"SANDBOX_AUTH_TOKEN={SESSION_TOKEN}" in environment
            assert SESSION_TOKEN not in argv
            assert SESSION_TOKEN not in command_line
        finally:
            await driver.delete(handle)


class TestLaunchEnvironment:
    async def test_names_the_directories_it_created(self, driver: LocalProcessDriver) -> None:
        """The runtime resolves platform defaults but never creates directories,
        so the launcher has to say where it put them."""
        handle = await driver.create(spec())
        try:
            directory = driver._directory("sandbox-1")
            environment = await wait_for(directory / "env.txt")

            assert f"OI_WORKSPACE_ROOT={directory / 'workspace'}" in environment
            assert f"OPENINSPECT_BIN_INSTALL_DIR={directory / 'bin'}" in environment
            assert (directory / "workspace").is_dir()
            assert (directory / "bin").is_dir()
        finally:
            await driver.delete(handle)

    async def test_puts_the_sandbox_bin_directory_first_on_path(
        self, driver: LocalProcessDriver
    ) -> None:
        """So the runtime's authenticated `gh` wrapper shadows the real one."""
        handle = await driver.create(spec())
        try:
            directory = driver._directory("sandbox-1")
            environment = await wait_for(directory / "env.txt")

            path_line = next(line for line in environment.splitlines() if line.startswith("PATH="))
            assert path_line.removeprefix("PATH=").startswith(f"{directory / 'bin'}:")
        finally:
            await driver.delete(handle)

    async def test_passes_the_control_planes_environment_through(
        self, driver: LocalProcessDriver
    ) -> None:
        handle = await driver.create(spec())
        try:
            environment = await wait_for(driver._directory("sandbox-1") / "env.txt")

            assert "REPO_NAME=storefront" in environment
            assert "SANDBOX_TIMEOUT_SECONDS=3600" in environment
        finally:
            await driver.delete(handle)


class TestSuspendAndResume:
    async def test_suspend_stops_the_process_and_keeps_the_workspace(
        self, driver: LocalProcessDriver
    ) -> None:
        handle = await driver.create(spec())
        await wait_for(driver._directory("sandbox-1") / "argv.txt")
        checkout = driver._directory("sandbox-1") / "workspace" / "storefront"
        checkout.mkdir()

        await driver.suspend(handle)

        assert await driver.is_running(handle) is False
        assert checkout.is_dir()

    async def test_resume_runs_again_against_the_same_workspace(
        self, driver: LocalProcessDriver
    ) -> None:
        """What survives a suspend is the sandbox's state, not its process."""
        handle = await driver.create(spec())
        await wait_for(driver._directory("sandbox-1") / "argv.txt")
        checkout = driver._directory("sandbox-1") / "workspace" / "storefront"
        checkout.mkdir()
        await driver.suspend(handle)

        resumed = await driver.resume(handle, spec())
        try:
            assert resumed == handle
            assert await driver.is_running(resumed) is True
            assert checkout.is_dir()
        finally:
            await driver.delete(resumed)

    async def test_refuses_to_resume_a_sandbox_whose_workspace_is_gone(
        self, driver: LocalProcessDriver
    ) -> None:
        handle = await driver.create(spec())
        await driver.delete(handle)

        with pytest.raises(DriverError, match="workspace is gone"):
            await driver.resume(handle, spec())


class TestDelete:
    async def test_stops_the_process_and_removes_the_workspace(
        self, driver: LocalProcessDriver
    ) -> None:
        handle = await driver.create(spec())
        await wait_for(driver._directory("sandbox-1") / "argv.txt")

        await driver.delete(handle)

        assert await driver.is_running(handle) is False
        assert driver._directory("sandbox-1").exists() is False

    async def test_deleting_twice_is_harmless(self, driver: LocalProcessDriver) -> None:
        handle = await driver.create(spec())

        await driver.delete(handle)
        await driver.delete(handle)


class TestExecute:
    async def test_runs_a_command_in_the_sandbox_directory(
        self, driver: LocalProcessDriver
    ) -> None:
        handle = await driver.create(spec())
        try:
            result = await driver.execute(handle, ["/bin/sh", "-c", "pwd"])

            assert result.exit_code == 0
            assert "sandbox-1" in result.stdout
        finally:
            await driver.delete(handle)

    async def test_reports_a_failing_command(self, driver: LocalProcessDriver) -> None:
        handle = await driver.create(spec())
        try:
            result = await driver.execute(handle, ["/bin/sh", "-c", "echo nope >&2; exit 3"])

            assert result.exit_code == 3
            assert result.stderr.strip() == "nope"
        finally:
            await driver.delete(handle)

    async def test_refuses_a_sandbox_that_has_no_directory(
        self, driver: LocalProcessDriver
    ) -> None:
        handle = await driver.create(spec())
        await driver.delete(handle)

        with pytest.raises(DriverError, match="No sandbox directory"):
            await driver.execute(handle, ["/bin/sh", "-c", "true"])


class TestRunningIds:
    async def test_reports_only_the_sandboxes_currently_executing(
        self, driver: LocalProcessDriver
    ) -> None:
        """Startup reconciliation compares this against the agent's own records."""
        first = await driver.create(spec(sandbox_id="sandbox-1"))
        second = await driver.create(spec(sandbox_id="sandbox-2"))
        try:
            assert await driver.running_ids() == frozenset({"sandbox-1", "sandbox-2"})

            await driver.suspend(second)

            assert await driver.running_ids() == frozenset({"sandbox-1"})
        finally:
            await driver.delete(first)
            await driver.delete(second)
