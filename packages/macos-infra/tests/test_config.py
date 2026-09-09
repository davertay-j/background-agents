"""A host that cannot be configured must say so at startup, once, in full."""

from __future__ import annotations

import pytest

from macos_host_agent.config import (
    API_KEY_ENV_VAR,
    DEFAULT_HOST,
    DEFAULT_PORT,
    MINIMUM_API_KEY_LENGTH,
    PORT_ENV_VAR,
    RUNTIME_PYTHON_ENV_VAR,
    SANDBOX_ROOT_ENV_VAR,
    ConfigurationError,
    HostAgentConfig,
)


def complete_environment(**overrides: str) -> dict[str, str]:
    environment = {
        API_KEY_ENV_VAR: "a" * MINIMUM_API_KEY_LENGTH,
        SANDBOX_ROOT_ENV_VAR: "/var/openinspect/sandboxes",
        RUNTIME_PYTHON_ENV_VAR: "/opt/openinspect/python/bin/python",
    }
    environment.update(overrides)
    return environment


class TestRequiredVariables:
    def test_names_every_missing_variable_at_once(self) -> None:
        with pytest.raises(ConfigurationError) as raised:
            HostAgentConfig.from_env({})

        message = str(raised.value)
        for name in (API_KEY_ENV_VAR, SANDBOX_ROOT_ENV_VAR, RUNTIME_PYTHON_ENV_VAR):
            assert name in message

    def test_treats_an_empty_value_as_missing(self) -> None:
        with pytest.raises(ConfigurationError, match=API_KEY_ENV_VAR):
            HostAgentConfig.from_env(complete_environment(**{API_KEY_ENV_VAR: ""}))

    def test_accepts_a_complete_environment(self) -> None:
        config = HostAgentConfig.from_env(complete_environment())

        assert config.host == DEFAULT_HOST
        assert config.port == DEFAULT_PORT
        assert config.sandbox_root.as_posix() == "/var/openinspect/sandboxes"


class TestMalformedValues:
    def test_refuses_a_short_api_key(self) -> None:
        """The key is also the HMAC secret for editor and desktop passwords."""
        with pytest.raises(ConfigurationError, match="at least"):
            HostAgentConfig.from_env(complete_environment(**{API_KEY_ENV_VAR: "short"}))

    @pytest.mark.parametrize("name", [SANDBOX_ROOT_ENV_VAR, RUNTIME_PYTHON_ENV_VAR])
    def test_refuses_a_relative_path(self, name: str) -> None:
        with pytest.raises(ConfigurationError, match="absolute"):
            HostAgentConfig.from_env(complete_environment(**{name: "relative/path"}))

    @pytest.mark.parametrize("raw", ["not-a-number", "0", "70000", "-1"])
    def test_refuses_a_port_that_is_not_a_port(self, raw: str) -> None:
        with pytest.raises(ConfigurationError, match=PORT_ENV_VAR):
            HostAgentConfig.from_env(complete_environment(**{PORT_ENV_VAR: raw}))

    def test_accepts_a_valid_port(self) -> None:
        config = HostAgentConfig.from_env(complete_environment(**{PORT_ENV_VAR: "9001"}))

        assert config.port == 9001


class TestSandboxPath:
    def test_defaults_to_a_path_naming_the_keg_only_node(self) -> None:
        """A setup hook that cannot find node fails the whole install step."""
        config = HostAgentConfig.from_env(complete_environment())

        assert "/opt/homebrew/opt/node@22/bin" in config.sandbox_path

    def test_can_be_overridden_for_a_differently_laid_out_host(self) -> None:
        config = HostAgentConfig.from_env(
            complete_environment(OI_HOST_AGENT_SANDBOX_PATH="/custom/bin:/usr/bin")
        )

        assert config.sandbox_path == "/custom/bin:/usr/bin"
