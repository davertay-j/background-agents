"""The host agent's HTTP surface.

A translation layer and nothing more: parse, authenticate, call the registry,
map failures onto status codes. The vocabulary follows the OpenComputer
provider's paths so the control-plane side stays thin in the same way.

The status codes are load-bearing rather than cosmetic. The control plane
classifies a provider failure as transient or permanent and trips a circuit
breaker on repeated permanent ones, and it derives that from the HTTP status.
So a host at its VM limit answers 503 -- routine, retryable, already transient
in the shared classifier -- while a host that is genuinely misconfigured
answers 500 and is allowed to trip the breaker.
"""

from __future__ import annotations

import hmac
from typing import TYPE_CHECKING, Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Response, status
from pydantic import BaseModel, Field

from .driver import CapacityExhausted, DriverError, SandboxDriver, SandboxSpec
from .local_process_driver import LocalProcessDriver
from .registry import SandboxAlreadyExists, SandboxRecord, SandboxRegistry, UnknownSandbox

if TYPE_CHECKING:
    from .config import HostAgentConfig

API_KEY_HEADER = "X-OI-Host-Agent-Key"


class CreateSandboxRequest(BaseModel):
    sandbox_id: str = Field(min_length=1)
    environment: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: int = Field(gt=0)
    image: str | None = None


class ExecRequest(BaseModel):
    argv: list[str] = Field(min_length=1)
    environment: dict[str, str] = Field(default_factory=dict)


class SetTimeoutRequest(BaseModel):
    timeout_seconds: int = Field(gt=0)


class SandboxResponse(BaseModel):
    sandbox_id: str
    status: str
    created_at_ms: int
    expires_at_ms: int
    timeout_seconds: int

    @classmethod
    def of(cls, record: SandboxRecord) -> SandboxResponse:
        return cls(
            sandbox_id=record.sandbox_id,
            status=str(record.status),
            created_at_ms=record.created_at_ms,
            expires_at_ms=record.expires_at_ms,
            timeout_seconds=record.timeout_seconds,
        )


class ExecResponse(BaseModel):
    exit_code: int
    stdout: str
    stderr: str


def create_app(config: HostAgentConfig, driver: SandboxDriver | None = None) -> FastAPI:
    """Build the service. Tests pass a fake driver; production passes none."""
    registry = SandboxRegistry(
        driver
        or LocalProcessDriver(
            sandbox_root=config.sandbox_root,
            runtime_python=config.runtime_python,
            sandbox_path=config.sandbox_path,
        )
    )

    def authenticate(
        presented: Annotated[str | None, Header(alias=API_KEY_HEADER)] = None,
    ) -> None:
        """Every sandbox route goes through here.

        The host sits on a trusted LAN, which is exactly the assumption not
        worth relying on: anything that can route to this port would otherwise
        be able to spawn a sandbox holding live repository credentials.
        """
        if presented is None or not hmac.compare_digest(presented, config.api_key):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="A valid host agent key is required",
            )

    app = FastAPI(title="Open-Inspect macOS host agent")
    authenticated = [Depends(authenticate)]

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        """Unauthenticated liveness, so a supervisor or tunnel can probe it.

        Reports nothing about any session -- only that the process is up and
        which driver it would use.
        """
        return {"status": "ok", "driver": registry.driver_name}

    @app.post("/sandboxes", dependencies=authenticated)
    async def create_sandbox(request: CreateSandboxRequest) -> SandboxResponse:
        spec = SandboxSpec(
            sandbox_id=request.sandbox_id,
            environment=request.environment,
            timeout_seconds=request.timeout_seconds,
            image=request.image,
        )
        try:
            return SandboxResponse.of(await registry.create(spec))
        except SandboxAlreadyExists:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Sandbox {request.sandbox_id} already exists",
            )
        except CapacityExhausted as error:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error))
        except DriverError as error:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(error)
            )

    @app.get("/sandboxes/{sandbox_id}", dependencies=authenticated)
    def get_sandbox(sandbox_id: str) -> SandboxResponse:
        return SandboxResponse.of(_found(registry, sandbox_id))

    @app.post("/sandboxes/{sandbox_id}/exec", dependencies=authenticated)
    async def exec_in_sandbox(sandbox_id: str, request: ExecRequest) -> ExecResponse:
        _found(registry, sandbox_id)
        try:
            result = await registry.execute(
                sandbox_id, request.argv, environment=request.environment
            )
        except DriverError as error:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(error)
            )
        return ExecResponse(exit_code=result.exit_code, stdout=result.stdout, stderr=result.stderr)

    @app.post("/sandboxes/{sandbox_id}/suspend", dependencies=authenticated)
    async def suspend_sandbox(sandbox_id: str) -> SandboxResponse:
        _found(registry, sandbox_id)
        return SandboxResponse.of(await registry.suspend(sandbox_id))

    @app.post("/sandboxes/{sandbox_id}/resume", dependencies=authenticated)
    async def resume_sandbox(sandbox_id: str) -> SandboxResponse:
        _found(registry, sandbox_id)
        try:
            return SandboxResponse.of(await registry.resume(sandbox_id))
        except CapacityExhausted as error:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error))
        except DriverError as error:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(error)
            )

    @app.post("/sandboxes/{sandbox_id}/timeout", dependencies=authenticated)
    def set_sandbox_timeout(sandbox_id: str, request: SetTimeoutRequest) -> SandboxResponse:
        _found(registry, sandbox_id)
        return SandboxResponse.of(registry.set_timeout(sandbox_id, request.timeout_seconds))

    @app.delete(
        "/sandboxes/{sandbox_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        dependencies=authenticated,
    )
    async def delete_sandbox(sandbox_id: str) -> Response:
        """Idempotent: deleting a sandbox the agent has already reaped succeeds."""
        await registry.delete(sandbox_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return app


def _found(registry: SandboxRegistry, sandbox_id: str) -> SandboxRecord:
    try:
        return registry.get(sandbox_id)
    except UnknownSandbox:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"No sandbox {sandbox_id}"
        )
