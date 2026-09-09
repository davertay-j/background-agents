"""Entry point: `python -m macos_host_agent.main`.

Configuration is resolved before the server is built, so a host that cannot be
configured exits with the reason on stderr instead of starting and failing the
first request.
"""

from __future__ import annotations

import os
import sys

import uvicorn

from .app import create_app
from .config import ConfigurationError, HostAgentConfig


def main() -> int:
    try:
        config = HostAgentConfig.from_env(os.environ)
    except ConfigurationError as error:
        print(f"macos-host-agent: {error}", file=sys.stderr)
        return 2

    config.sandbox_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    uvicorn.run(create_app(config), host=config.host, port=config.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
