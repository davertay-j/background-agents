"""The host agent's configuration, read from the process environment.

Every value is resolved once at startup and a boot that cannot be configured
fails naming every missing variable at once, so one restart reports the whole
gap rather than one variable per attempt. That matters more here than in a
cloud service: the operator is often on the far end of an SSH session to a
machine in a cupboard.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

API_KEY_ENV_VAR = "OI_HOST_AGENT_API_KEY"
SANDBOX_ROOT_ENV_VAR = "OI_HOST_AGENT_SANDBOX_ROOT"
RUNTIME_PYTHON_ENV_VAR = "OI_HOST_AGENT_RUNTIME_PYTHON"
SANDBOX_PATH_ENV_VAR = "OI_HOST_AGENT_SANDBOX_PATH"
HOST_ENV_VAR = "OI_HOST_AGENT_HOST"
PORT_ENV_VAR = "OI_HOST_AGENT_PORT"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8900
"""A pre-shared key shorter than this is refused rather than merely warned about."""
MINIMUM_API_KEY_LENGTH = 32


class ConfigurationError(Exception):
    """The environment cannot configure a host agent."""


@dataclass(frozen=True)
class HostAgentConfig:
    """What the service needs to answer requests and launch sandboxes."""

    api_key: str
    """Pre-shared key every request must carry. Also the HMAC secret the control
    plane derives browser-editor and desktop passwords from, so it is a real
    secret and not merely a network guard."""

    sandbox_root: Path
    """Parent of the per-sandbox scratch directories. Each sandbox owns one
    subdirectory and never reads outside it."""

    runtime_python: Path
    """Interpreter with the sandbox runtime installed. The runtime spawns its
    own bridge off `sys.executable`, so this one choice settles both."""

    sandbox_path: str
    """PATH for sandbox processes. The launcher owns this because it is the only
    party that knows how the host is laid out -- where Homebrew put node, which
    prefix carries the coding agent. The runtime deliberately does not guess."""

    host: str
    port: int

    @classmethod
    def from_env(cls, environment: Mapping[str, str]) -> HostAgentConfig:
        missing = [
            name
            for name in (API_KEY_ENV_VAR, SANDBOX_ROOT_ENV_VAR, RUNTIME_PYTHON_ENV_VAR)
            if not environment.get(name)
        ]
        if missing:
            raise ConfigurationError(f"Missing required configuration: {', '.join(missing)}")

        api_key = environment[API_KEY_ENV_VAR]
        if len(api_key) < MINIMUM_API_KEY_LENGTH:
            raise ConfigurationError(
                f"{API_KEY_ENV_VAR} must be at least {MINIMUM_API_KEY_LENGTH} characters; "
                f"got {len(api_key)}"
            )

        runtime_python = Path(environment[RUNTIME_PYTHON_ENV_VAR])
        if not runtime_python.is_absolute():
            raise ConfigurationError(f"{RUNTIME_PYTHON_ENV_VAR} must be an absolute path")

        sandbox_root = Path(environment[SANDBOX_ROOT_ENV_VAR])
        if not sandbox_root.is_absolute():
            raise ConfigurationError(f"{SANDBOX_ROOT_ENV_VAR} must be an absolute path")

        return cls(
            api_key=api_key,
            sandbox_root=sandbox_root,
            runtime_python=runtime_python,
            sandbox_path=environment.get(SANDBOX_PATH_ENV_VAR) or _default_sandbox_path(),
            host=environment.get(HOST_ENV_VAR) or DEFAULT_HOST,
            port=_port(environment.get(PORT_ENV_VAR)),
        )


def _port(raw: str | None) -> int:
    if not raw:
        return DEFAULT_PORT
    if not raw.isdigit():
        raise ConfigurationError(f"{PORT_ENV_VAR} must be a positive integer; got {raw!r}")
    port = int(raw)
    if not 1 <= port <= 65535:
        raise ConfigurationError(f"{PORT_ENV_VAR} must be between 1 and 65535; got {port}")
    return port


def _default_sandbox_path() -> str:
    """A PATH covering where Homebrew puts things on Apple silicon.

    `node@22` is keg-only, so its own bin directory has to be named: a
    repository's setup hook checks for node before doing anything, and a hook
    that cannot find it fails the whole install step.
    """
    return ":".join(
        (
            "/opt/homebrew/opt/node@22/bin",
            "/opt/homebrew/bin",
            "/usr/local/bin",
            "/usr/bin",
            "/bin",
            "/usr/sbin",
            "/sbin",
        )
    )
