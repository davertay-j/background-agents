"""Filesystem locations and services that differ by host operating system.

The runtime grew up inside the Debian image every Linux backend boots, where
the workspace is a volume mounted at /workspace, `gh` is a system package at
/usr/bin/gh, and /run/oi is a tmpfs holding the SCM credential cache. A macOS
sandbox shares none of that: the root volume is read-only, Homebrew installs
gh under its own prefix, and /run does not exist.

Rather than scatter `if darwin` branches through the services, each supported
system contributes one `RuntimePlatform`. The entrypoint detects the platform
once and threads its `PlatformPaths` into the services it composes, so a
service never asks which system it is running on.

`constants` stays the home of the Linux values. They are a published contract
-- the control plane's Vercel provider, the image install scripts under
packages/sandbox-images/install, and plugins/inspect-plugin.js all name the
same paths -- so `LinuxPlatform` reads them back rather than restating them.
"""

from __future__ import annotations

import os
import platform
import shutil
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from .browser_desktop import BrowserDesktop, DisabledBrowserDesktop
from .constants import (
    BIN_INSTALL_DIR_ENV_VAR,
    DEFAULT_BIN_INSTALL_DIR,
    DEFAULT_GH_EXECUTABLE,
    DEFAULT_SCM_CRED_CACHE_DIR,
    DEFAULT_WORKSPACE_ROOT,
    SCM_CRED_CACHE_DIR_ENV_VAR,
    TUNNEL_ENV_FILE_NAME,
    WORKSPACE_ROOT_ENV_VAR,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .browser_desktop import BrowserDesktopService

LINUX = "Linux"
DARWIN = "Darwin"


@dataclass(frozen=True)
class PlatformPaths:
    """Where this platform keeps what the runtime reads and writes."""

    # Parent of every repository checkout.
    workspace_root: Path
    # Holds the short-lived SCM credentials the git credential helper mints.
    credential_cache_dir: Path
    # Receives the runtime's standalone CLIs, including the gh wrapper. On PATH.
    bin_install_dir: Path
    # The real gh the wrapper delegates to, or None where there is no gh to wrap.
    gh_executable: Path | None

    @property
    def tunnel_env_file(self) -> Path:
        return self.workspace_root / TUNNEL_ENV_FILE_NAME


class RuntimePlatform(ABC):
    """One host system's answer to where things live and which services run."""

    name: ClassVar[str]

    def __init__(self, environment: Mapping[str, str] | None = None) -> None:
        self._environment: Mapping[str, str] = os.environ if environment is None else environment
        self.paths = self._resolve_paths()

    @abstractmethod
    def _resolve_paths(self) -> PlatformPaths: ...

    @abstractmethod
    def create_browser_desktop(self, log: Any, *, password: str | None) -> BrowserDesktopService:
        """Build the desktop the agent's browser draws into."""

    def _configured(self, env_var: str, default: str | Path) -> Path:
        """A location the environment may override, else this platform's default.

        Whoever launches the sandbox owns these variables -- that is how the
        Linux backends already point the runtime at provider-specific
        directories -- so a value they set always wins over a default here.
        """
        return Path(self._environment.get(env_var) or default)

    def _home(self) -> Path:
        return Path(self._environment.get("HOME") or Path.home())


class LinuxPlatform(RuntimePlatform):
    """The Debian sandbox image shared by every Linux backend."""

    name = LINUX

    def _resolve_paths(self) -> PlatformPaths:
        return PlatformPaths(
            workspace_root=self._configured(WORKSPACE_ROOT_ENV_VAR, DEFAULT_WORKSPACE_ROOT),
            credential_cache_dir=self._configured(
                SCM_CRED_CACHE_DIR_ENV_VAR, DEFAULT_SCM_CRED_CACHE_DIR
            ),
            bin_install_dir=self._configured(BIN_INSTALL_DIR_ENV_VAR, DEFAULT_BIN_INSTALL_DIR),
            gh_executable=Path(DEFAULT_GH_EXECUTABLE),
        )

    def create_browser_desktop(self, log: Any, *, password: str | None) -> BrowserDesktopService:
        return BrowserDesktop(log, password=password)


class DarwinPlatform(RuntimePlatform):
    """A macOS VM, where nothing at the root of the filesystem is writable."""

    name = DARWIN

    def _resolve_paths(self) -> PlatformPaths:
        bin_install_dir = self._configured(BIN_INSTALL_DIR_ENV_VAR, self._home() / ".local" / "bin")
        return PlatformPaths(
            workspace_root=self._configured(WORKSPACE_ROOT_ENV_VAR, self._home() / "workspace"),
            # $TMPDIR is the closest analogue to the image's /run tmpfs: private
            # to the user, cleared by the system, and outside every backup.
            credential_cache_dir=self._configured(
                SCM_CRED_CACHE_DIR_ENV_VAR, self._temp_dir() / "oi"
            ),
            bin_install_dir=bin_install_dir,
            gh_executable=self._resolve_gh_executable(bin_install_dir),
        )

    def create_browser_desktop(self, log: Any, *, password: str | None) -> BrowserDesktopService:
        # The X11 stack the Linux desktop drives (Xvfb, fluxbox, x11vnc,
        # websockify) has no macOS counterpart yet. Until one exists, a macOS
        # session runs without a desktop instead of failing to boot.
        return DisabledBrowserDesktop(log, reason="unsupported_on_darwin")

    def _temp_dir(self) -> Path:
        return Path(self._environment.get("TMPDIR") or tempfile.gettempdir())

    def _resolve_gh_executable(self, bin_install_dir: Path) -> Path | None:
        """Find the real gh on PATH, skipping the wrapper the runtime installs.

        Homebrew's prefix varies by architecture, so there is no fixed path to
        name the way Debian's package gives us one. The wrapper is itself named
        `gh` and lands in bin_install_dir, which is on PATH -- resolving that as
        the real gh would have it exec itself.
        """
        search_path = os.pathsep.join(
            entry
            for entry in self._environment.get("PATH", "").split(os.pathsep)
            if entry and Path(entry) != bin_install_dir
        )
        found = shutil.which("gh", path=search_path)
        return Path(found) if found else None


_PLATFORMS: dict[str, type[RuntimePlatform]] = {
    LINUX: LinuxPlatform,
    DARWIN: DarwinPlatform,
}


def detect_runtime_platform(
    *,
    system: str | None = None,
    environment: Mapping[str, str] | None = None,
) -> RuntimePlatform:
    """Build the platform for the system this process is running on."""
    resolved_system = platform.system() if system is None else system
    implementation = _PLATFORMS.get(resolved_system)
    if implementation is None:
        supported = ", ".join(sorted(_PLATFORMS))
        raise RuntimeError(
            f"The sandbox runtime does not support {resolved_system or 'this system'}; "
            f"it runs on {supported}"
        )
    return implementation(environment)
