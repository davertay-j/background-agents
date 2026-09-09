"""Nothing on the network gets to spawn a sandbox without the key.

A sandbox holds live repository credentials, so an unauthenticated create is
not merely an unwanted process -- it is a way to be handed a token.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

from macos_host_agent.app import API_KEY_HEADER, create_app

from .conftest import API_KEY, FakeDriver

if TYPE_CHECKING:
    from macos_host_agent.config import HostAgentConfig

SANDBOX_ROUTES = (
    ("post", "/sandboxes"),
    ("get", "/sandboxes/sandbox-1"),
    ("post", "/sandboxes/sandbox-1/exec"),
    ("post", "/sandboxes/sandbox-1/suspend"),
    ("post", "/sandboxes/sandbox-1/resume"),
    ("post", "/sandboxes/sandbox-1/timeout"),
    ("delete", "/sandboxes/sandbox-1"),
)


@pytest.fixture
def anonymous(config: HostAgentConfig, driver: FakeDriver) -> TestClient:
    return TestClient(create_app(config, driver))


class TestUnauthenticatedRequests:
    @pytest.mark.parametrize(("method", "path"), SANDBOX_ROUTES)
    def test_every_sandbox_route_refuses_a_request_with_no_key(
        self, anonymous: TestClient, method: str, path: str
    ) -> None:
        response = anonymous.request(method, path, json={})

        assert response.status_code == 401

    @pytest.mark.parametrize(("method", "path"), SANDBOX_ROUTES)
    def test_every_sandbox_route_refuses_a_wrong_key(
        self, anonymous: TestClient, method: str, path: str
    ) -> None:
        response = anonymous.request(
            method, path, json={}, headers={API_KEY_HEADER: "x" * len(API_KEY)}
        )

        assert response.status_code == 401

    def test_refuses_a_key_that_is_a_prefix_of_the_real_one(self, anonymous: TestClient) -> None:
        response = anonymous.get("/sandboxes/sandbox-1", headers={API_KEY_HEADER: API_KEY[:-1]})

        assert response.status_code == 401

    def test_rejects_before_doing_any_work(self, anonymous: TestClient, driver: FakeDriver) -> None:
        """A rejected create must not have reached the driver."""
        anonymous.post(
            "/sandboxes",
            json={"sandbox_id": "sandbox-1", "timeout_seconds": 60, "environment": {}},
        )

        assert driver.created == []


class TestHealth:
    def test_liveness_is_open_and_tells_nothing_about_sessions(self, anonymous: TestClient) -> None:
        """Open so a supervisor or tunnel can probe it without holding the key."""
        response = anonymous.get("/healthz")

        assert response.status_code == 200
        assert response.json() == {"status": "ok", "driver": "fake"}
