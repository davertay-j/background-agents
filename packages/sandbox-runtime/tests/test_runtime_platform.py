"""Tests for per-platform path resolution and service selection.

The Linux cases are regression tests as much as unit tests: five backends boot
the Debian image, so LinuxPlatform must keep resolving exactly the paths the
shared constants publish.
"""

from __future__ import annotations

import platform as platform_module
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from sandbox_runtime.browser_desktop import BrowserDesktop, DisabledBrowserDesktop
from sandbox_runtime.constants import (
    BIN_INSTALL_DIR_ENV_VAR,
    SCM_CRED_CACHE_DIR_ENV_VAR,
    WORKSPACE_ROOT_ENV_VAR,
)
from sandbox_runtime.runtime_platform import (
    DARWIN,
    LINUX,
    DarwinPlatform,
    LinuxPlatform,
    detect_runtime_platform,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

DARWIN_ENV = {"HOME": "/Users/sandbox", "TMPDIR": "/var/folders/ab/T/"}


def _executable(directory: Path, name: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    command = directory / name
    command.write_text("#!/bin/sh\n")
    command.chmod(0o755)
    return command


class TestPlatformSelection:
    def test_selects_implementation_per_system(self) -> None:
        assert isinstance(detect_runtime_platform(system=LINUX, environment={}), LinuxPlatform)
        assert isinstance(
            detect_runtime_platform(system=DARWIN, environment=DARWIN_ENV), DarwinPlatform
        )

    def test_detects_the_running_system_by_default(self) -> None:
        assert detect_runtime_platform().name == platform_module.system()

    def test_rejects_an_unsupported_system_naming_the_supported_ones(self) -> None:
        with pytest.raises(RuntimeError, match=r"does not support Windows.*Darwin, Linux"):
            detect_runtime_platform(system="Windows", environment={})


class TestLinuxPaths:
    """The image's layout, which every Linux backend already depends on."""

    def test_resolves_the_debian_image_layout(self) -> None:
        paths = LinuxPlatform({}).paths

        assert paths.workspace_root == Path("/workspace")
        assert paths.credential_cache_dir == Path("/run/oi")
        assert paths.bin_install_dir == Path("/usr/local/bin")
        assert paths.gh_executable == Path("/usr/bin/gh")

    def test_tunnel_env_file_sits_at_the_workspace_root(self) -> None:
        assert LinuxPlatform({}).paths.tunnel_env_file == Path("/workspace/.tunnels.env")

    @pytest.mark.parametrize(
        ("env_var", "attribute"),
        [
            (WORKSPACE_ROOT_ENV_VAR, "workspace_root"),
            (SCM_CRED_CACHE_DIR_ENV_VAR, "credential_cache_dir"),
            (BIN_INSTALL_DIR_ENV_VAR, "bin_install_dir"),
        ],
    )
    def test_launcher_configuration_wins_over_the_default(
        self, env_var: str, attribute: str
    ) -> None:
        """Providers already retarget these; the platform must not override them."""
        paths = LinuxPlatform({env_var: "/provider/chosen"}).paths

        assert getattr(paths, attribute) == Path("/provider/chosen")

    def test_tunnel_env_file_follows_a_configured_workspace_root(self) -> None:
        paths = LinuxPlatform({WORKSPACE_ROOT_ENV_VAR: "/mnt/work"}).paths

        assert paths.tunnel_env_file == Path("/mnt/work/.tunnels.env")


class TestDarwinPaths:
    def test_resolves_everything_below_home_and_tmpdir(self) -> None:
        paths = DarwinPlatform(DARWIN_ENV).paths

        assert paths.workspace_root == Path("/Users/sandbox/workspace")
        assert paths.bin_install_dir == Path("/Users/sandbox/.local/bin")
        assert paths.credential_cache_dir == Path("/var/folders/ab/T/oi")

    def test_writes_nothing_below_the_read_only_root(self) -> None:
        """The macOS root volume is read-only, so no resolved path may live there."""
        paths = DarwinPlatform(DARWIN_ENV).paths

        for path in (
            paths.workspace_root,
            paths.bin_install_dir,
            paths.credential_cache_dir,
            paths.tunnel_env_file,
        ):
            assert not path.is_relative_to("/workspace")
            assert not path.is_relative_to("/run")
            assert not path.is_relative_to("/usr")

    def test_tunnel_env_file_sits_at_the_workspace_root(self) -> None:
        paths = DarwinPlatform(DARWIN_ENV).paths

        assert paths.tunnel_env_file == Path("/Users/sandbox/workspace/.tunnels.env")

    @pytest.mark.parametrize(
        ("env_var", "attribute"),
        [
            (WORKSPACE_ROOT_ENV_VAR, "workspace_root"),
            (SCM_CRED_CACHE_DIR_ENV_VAR, "credential_cache_dir"),
            (BIN_INSTALL_DIR_ENV_VAR, "bin_install_dir"),
        ],
    )
    def test_launcher_configuration_wins_over_the_default(
        self, env_var: str, attribute: str
    ) -> None:
        paths = DarwinPlatform({**DARWIN_ENV, env_var: "/Volumes/oi/chosen"}).paths

        assert getattr(paths, attribute) == Path("/Volumes/oi/chosen")


class TestDarwinGhResolution:
    """macOS has no fixed gh path, so it is resolved off PATH."""

    def _environment(self, *directories: Path, home: Path) -> Mapping[str, str]:
        return {**DARWIN_ENV, "HOME": str(home), "PATH": ":".join(str(d) for d in directories)}

    def test_finds_gh_on_path(self, tmp_path: Path) -> None:
        brew_bin = tmp_path / "opt" / "homebrew" / "bin"
        real_gh = _executable(brew_bin, "gh")

        platform = DarwinPlatform(self._environment(brew_bin, home=tmp_path / "home"))

        assert platform.paths.gh_executable == real_gh

    def test_never_resolves_the_wrapper_it_installs(self, tmp_path: Path) -> None:
        """The wrapper is named gh and lands on PATH; resolving it would recurse."""
        home = tmp_path / "home"
        wrapper = _executable(home / ".local" / "bin", "gh")
        brew_bin = tmp_path / "opt" / "homebrew" / "bin"
        real_gh = _executable(brew_bin, "gh")

        platform = DarwinPlatform(self._environment(wrapper.parent, brew_bin, home=home))

        assert platform.paths.gh_executable == real_gh

    def test_returns_none_when_only_the_wrapper_is_on_path(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        wrapper = _executable(home / ".local" / "bin", "gh")

        platform = DarwinPlatform(self._environment(wrapper.parent, home=home))

        assert platform.paths.gh_executable is None

    def test_returns_none_when_gh_is_not_installed(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()

        platform = DarwinPlatform(self._environment(empty, home=tmp_path / "home"))

        assert platform.paths.gh_executable is None


class TestBrowserDesktopSelection:
    def test_linux_drives_the_x11_stack(self) -> None:
        desktop = LinuxPlatform({}).create_browser_desktop(MagicMock(), password="secret12")

        assert isinstance(desktop, BrowserDesktop)
        assert desktop._password == "secret12"

    def test_darwin_has_no_desktop(self) -> None:
        desktop = DarwinPlatform(DARWIN_ENV).create_browser_desktop(
            MagicMock(), password="secret12"
        )

        assert isinstance(desktop, DisabledBrowserDesktop)

    async def test_darwin_desktop_starts_and_stops_without_a_process(self) -> None:
        log = MagicMock()
        desktop = DarwinPlatform(DARWIN_ENV).create_browser_desktop(log, password="secret12")

        await desktop.start()
        await desktop.stop()

        log.info.assert_called_once_with("vnc.skip", reason="unsupported_on_darwin")

    def test_darwin_desktop_never_reports_a_crash_to_restart(self) -> None:
        """A desktop that was never started must not enter the supervisor's restart loop."""
        desktop = DarwinPlatform(DARWIN_ENV).create_browser_desktop(MagicMock(), password=None)

        assert desktop.crash() is None
