"""The host agent's record of its sandboxes, and the transitions it allows.

The lifecycle rules live here rather than in the HTTP layer so they can be
tested without a client, and so `app` stays a translation from requests to
these calls. The driver is asked to do the work; this module decides whether
the work is legal and what the sandbox's state becomes.

One state is deliberately absent: there is no "gone but remembered". A deleted
sandbox is forgotten, which makes `delete` idempotent -- the control plane may
well stop a sandbox the agent has already reaped, and that is not an error.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from .driver import DriverHandle, ExecResult, SandboxDriver, SandboxSpec


class SandboxStatus(StrEnum):
    RUNNING = "running"
    SUSPENDED = "suspended"


class UnknownSandbox(Exception):
    """No sandbox by that id."""


class SandboxAlreadyExists(Exception):
    """A live sandbox already holds that id."""


@dataclass(frozen=True)
class SandboxRecord:
    sandbox_id: str
    handle: DriverHandle
    spec: SandboxSpec
    status: SandboxStatus
    created_at_ms: int
    expires_at_ms: int

    @property
    def timeout_seconds(self) -> int:
        return self.spec.timeout_seconds


class SandboxRegistry:
    """Every sandbox this agent is responsible for."""

    def __init__(self, driver: SandboxDriver, *, now_ms: Callable[[], int] | None = None) -> None:
        self._driver = driver
        self._records: dict[str, SandboxRecord] = {}
        # One operation per sandbox at a time. Suspend and resume both take
        # real time, and two of them interleaving would leave the record
        # describing a sandbox that is not in that state.
        self._locks: dict[str, asyncio.Lock] = {}
        self._now_ms = now_ms or _now_ms

    @property
    def driver_name(self) -> str:
        return self._driver.name

    def get(self, sandbox_id: str) -> SandboxRecord:
        record = self._records.get(sandbox_id)
        if record is None:
            raise UnknownSandbox(sandbox_id)
        return record

    def all(self) -> tuple[SandboxRecord, ...]:
        return tuple(self._records.values())

    async def create(self, spec: SandboxSpec) -> SandboxRecord:
        if spec.sandbox_id in self._records:
            raise SandboxAlreadyExists(spec.sandbox_id)
        async with self._lock(spec.sandbox_id):
            handle = await self._driver.create(spec)
            created = self._now_ms()
            record = SandboxRecord(
                sandbox_id=spec.sandbox_id,
                handle=handle,
                spec=spec,
                status=SandboxStatus.RUNNING,
                created_at_ms=created,
                expires_at_ms=created + spec.timeout_seconds * 1000,
            )
            self._records[spec.sandbox_id] = record
            return record

    async def suspend(self, sandbox_id: str) -> SandboxRecord:
        async with self._lock(sandbox_id):
            record = self.get(sandbox_id)
            if record.status is SandboxStatus.SUSPENDED:
                return record
            await self._driver.suspend(record.handle)
            return self._store(replace(record, status=SandboxStatus.SUSPENDED))

    async def resume(self, sandbox_id: str) -> SandboxRecord:
        async with self._lock(sandbox_id):
            record = self.get(sandbox_id)
            if record.status is SandboxStatus.RUNNING:
                return record
            handle = await self._driver.resume(record.handle, record.spec)
            return self._store(replace(record, handle=handle, status=SandboxStatus.RUNNING))

    async def execute(
        self, sandbox_id: str, argv: Sequence[str], *, environment: dict[str, str] | None = None
    ) -> ExecResult:
        record = self.get(sandbox_id)
        return await self._driver.execute(record.handle, argv, environment=environment)

    async def delete(self, sandbox_id: str) -> None:
        """Tear the sandbox down. Deleting one that is already gone is not an error."""
        record = self._records.get(sandbox_id)
        if record is None:
            return
        async with self._lock(sandbox_id):
            await self._driver.delete(record.handle)
            self._records.pop(sandbox_id, None)
        self._locks.pop(sandbox_id, None)

    def set_timeout(self, sandbox_id: str, timeout_seconds: int) -> SandboxRecord:
        """Re-base the sandbox's lifetime on now.

        Recording only: nothing here acts on the deadline yet. Suspending an
        idle sandbox is CON-177, and it is the control plane that drives
        inactivity today.
        """
        record = self.get(sandbox_id)
        return self._store(
            replace(
                record,
                spec=replace(record.spec, timeout_seconds=timeout_seconds),
                expires_at_ms=self._now_ms() + timeout_seconds * 1000,
            )
        )

    async def is_running(self, sandbox_id: str) -> bool:
        return await self._driver.is_running(self.get(sandbox_id).handle)

    def _store(self, record: SandboxRecord) -> SandboxRecord:
        self._records[record.sandbox_id] = record
        return record

    def _lock(self, sandbox_id: str) -> asyncio.Lock:
        return self._locks.setdefault(sandbox_id, asyncio.Lock())


def _now_ms() -> int:
    return int(time.time() * 1000)
