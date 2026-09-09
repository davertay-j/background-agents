"""The lifecycle the control-plane provider drives, and how failures surface.

The important pair is suspend and resume. The provider declares persistent
resume, which means a stopped session's next prompt has to land on the same
sandbox rather than a fresh one -- so a resume that quietly created a new
sandbox would satisfy the HTTP contract and break the promise.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from macos_host_agent.driver import CapacityExhausted, DriverError

if TYPE_CHECKING:
    from fastapi.testclient import TestClient

    from .conftest import FakeDriver

CREATE_BODY = {
    "sandbox_id": "sandbox-1",
    "timeout_seconds": 3600,
    "environment": {"SANDBOX_AUTH_TOKEN": "token-abc", "REPO_NAME": "storefront"},
}


def create(client: TestClient, **overrides: object) -> dict[str, object]:
    response = client.post("/sandboxes", json={**CREATE_BODY, **overrides})
    assert response.status_code == 200, response.text
    body: dict[str, object] = response.json()
    return body


class TestCreate:
    def test_reports_the_sandbox_running_with_a_deadline(self, client: TestClient) -> None:
        body = create(client)

        assert body["sandbox_id"] == "sandbox-1"
        assert body["status"] == "running"
        assert body["timeout_seconds"] == 3600
        assert int(body["expires_at_ms"]) > int(body["created_at_ms"])  # type: ignore[call-overload]

    def test_hands_the_environment_to_the_driver_unchanged(
        self, client: TestClient, driver: FakeDriver
    ) -> None:
        create(client)

        assert driver.created[0].environment["SANDBOX_AUTH_TOKEN"] == "token-abc"
        assert driver.created[0].timeout_seconds == 3600

    def test_refuses_a_second_sandbox_with_the_same_id(self, client: TestClient) -> None:
        create(client)

        response = client.post("/sandboxes", json=CREATE_BODY)

        assert response.status_code == 409

    def test_reports_capacity_exhaustion_as_retryable(
        self, client: TestClient, driver: FakeDriver
    ) -> None:
        """503 is already transient in the control plane's shared classifier, so
        a host at its VM limit does not count towards the spawn circuit breaker."""
        driver.fail_create_with = CapacityExhausted("Both VM slots are in use")

        response = client.post("/sandboxes", json=CREATE_BODY)

        assert response.status_code == 503
        assert "VM slots" in response.json()["detail"]

    def test_reports_a_broken_host_as_permanent(
        self, client: TestClient, driver: FakeDriver
    ) -> None:
        driver.fail_create_with = DriverError("Could not start the sandbox runtime")

        response = client.post("/sandboxes", json=CREATE_BODY)

        assert response.status_code == 500

    def test_refuses_a_request_with_no_timeout(self, client: TestClient) -> None:
        response = client.post("/sandboxes", json={"sandbox_id": "sandbox-1"})

        assert response.status_code == 422


class TestGet:
    def test_reports_an_unknown_sandbox_as_missing(self, client: TestClient) -> None:
        response = client.get("/sandboxes/never-existed")

        assert response.status_code == 404

    def test_reports_a_created_sandbox(self, client: TestClient) -> None:
        create(client)

        response = client.get("/sandboxes/sandbox-1")

        assert response.status_code == 200
        assert response.json()["status"] == "running"


class TestSuspendAndResume:
    def test_suspend_stops_the_sandbox_without_deleting_it(
        self, client: TestClient, driver: FakeDriver
    ) -> None:
        create(client)

        response = client.post("/sandboxes/sandbox-1/suspend")

        assert response.status_code == 200
        assert response.json()["status"] == "suspended"
        assert driver.suspended == ["fake-sandbox-1"]
        assert driver.deleted == []

    def test_suspending_an_already_suspended_sandbox_changes_nothing(
        self, client: TestClient, driver: FakeDriver
    ) -> None:
        create(client)
        client.post("/sandboxes/sandbox-1/suspend")

        response = client.post("/sandboxes/sandbox-1/suspend")

        assert response.status_code == 200
        assert driver.suspended == ["fake-sandbox-1"]

    def test_resume_returns_to_the_same_sandbox_rather_than_a_new_one(
        self, client: TestClient, driver: FakeDriver
    ) -> None:
        created = create(client)
        client.post("/sandboxes/sandbox-1/suspend")

        response = client.post("/sandboxes/sandbox-1/resume")

        assert response.status_code == 200
        assert response.json()["status"] == "running"
        assert response.json()["created_at_ms"] == created["created_at_ms"]
        assert [spec.sandbox_id for spec in driver.resumed] == ["sandbox-1"]
        assert len(driver.created) == 1

    def test_resume_hands_back_the_environment_the_sandbox_was_created_with(
        self, client: TestClient, driver: FakeDriver
    ) -> None:
        """A driver that has to relaunch a process needs it; one that resumes a
        real VM ignores it."""
        create(client)
        client.post("/sandboxes/sandbox-1/suspend")

        client.post("/sandboxes/sandbox-1/resume")

        assert driver.resumed[0].environment["SANDBOX_AUTH_TOKEN"] == "token-abc"

    def test_resuming_a_running_sandbox_changes_nothing(
        self, client: TestClient, driver: FakeDriver
    ) -> None:
        create(client)

        response = client.post("/sandboxes/sandbox-1/resume")

        assert response.status_code == 200
        assert driver.resumed == []

    def test_reports_capacity_exhaustion_on_resume_as_retryable(
        self, client: TestClient, driver: FakeDriver
    ) -> None:
        create(client)
        client.post("/sandboxes/sandbox-1/suspend")
        driver.fail_resume_with = CapacityExhausted("Both VM slots are in use")

        response = client.post("/sandboxes/sandbox-1/resume")

        assert response.status_code == 503

    def test_refuses_to_suspend_an_unknown_sandbox(self, client: TestClient) -> None:
        assert client.post("/sandboxes/never-existed/suspend").status_code == 404

    def test_refuses_to_resume_an_unknown_sandbox(self, client: TestClient) -> None:
        assert client.post("/sandboxes/never-existed/resume").status_code == 404


class TestSetTimeout:
    def test_extends_the_deadline(self, client: TestClient) -> None:
        created = create(client)

        response = client.post("/sandboxes/sandbox-1/timeout", json={"timeout_seconds": 7200})

        assert response.status_code == 200
        assert response.json()["timeout_seconds"] == 7200
        assert int(response.json()["expires_at_ms"]) > int(created["expires_at_ms"])  # type: ignore[call-overload]

    def test_refuses_an_unknown_sandbox(self, client: TestClient) -> None:
        response = client.post("/sandboxes/never-existed/timeout", json={"timeout_seconds": 60})

        assert response.status_code == 404

    def test_refuses_a_non_positive_timeout(self, client: TestClient) -> None:
        create(client)

        response = client.post("/sandboxes/sandbox-1/timeout", json={"timeout_seconds": 0})

        assert response.status_code == 422


class TestDelete:
    def test_tears_the_sandbox_down_and_forgets_it(
        self, client: TestClient, driver: FakeDriver
    ) -> None:
        create(client)

        response = client.delete("/sandboxes/sandbox-1")

        assert response.status_code == 204
        assert driver.deleted == ["fake-sandbox-1"]
        assert client.get("/sandboxes/sandbox-1").status_code == 404

    def test_deleting_a_sandbox_that_is_already_gone_succeeds(self, client: TestClient) -> None:
        """The control plane may well stop one the agent has already reaped."""
        assert client.delete("/sandboxes/never-existed").status_code == 204

    def test_the_id_can_be_used_again_after_a_delete(self, client: TestClient) -> None:
        create(client)
        client.delete("/sandboxes/sandbox-1")

        assert client.post("/sandboxes", json=CREATE_BODY).status_code == 200


class TestExec:
    def test_runs_the_command_in_the_sandbox(self, client: TestClient, driver: FakeDriver) -> None:
        create(client)

        response = client.post(
            "/sandboxes/sandbox-1/exec", json={"argv": ["sw_vers", "-productVersion"]}
        )

        assert response.status_code == 200
        assert response.json()["exit_code"] == 0
        assert driver.executed[0][1] == ("sw_vers", "-productVersion")

    def test_refuses_an_unknown_sandbox(self, client: TestClient) -> None:
        response = client.post("/sandboxes/never-existed/exec", json={"argv": ["true"]})

        assert response.status_code == 404

    def test_refuses_an_empty_command(self, client: TestClient) -> None:
        create(client)

        response = client.post("/sandboxes/sandbox-1/exec", json={"argv": []})

        assert response.status_code == 422
