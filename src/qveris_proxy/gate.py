from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator


class ConfigurationLease:
    __slots__ = ("_gate", "_release_task")

    def __init__(self, gate: ConfigurationGate) -> None:
        self._gate = gate
        self._release_task: asyncio.Task[None] | None = None

    @property
    def released(self) -> bool:
        return self._release_task is not None

    async def release(self) -> None:
        if self._release_task is None:
            self._release_task = asyncio.create_task(self._gate._release())
        await asyncio.shield(self._release_task)

    async def __aenter__(self) -> ConfigurationLease:
        if self.released:
            raise RuntimeError("configuration lease is already released")
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.release()


class ConfigurationGate:
    """An atomic update gate that keeps reads flowing while an update waits."""

    def __init__(self) -> None:
        self._condition = asyncio.Condition()
        self._active = 0
        self._updating = False
        self._waiting_updates = 0

    async def acquire(self) -> ConfigurationLease:
        async with self._condition:
            while self._updating:
                await self._condition.wait()
            self._active += 1
        return ConfigurationLease(self)

    @asynccontextmanager
    async def update(self) -> AsyncIterator[None]:
        async with self._condition:
            self._waiting_updates += 1
            owns_update = False
            try:
                while self._updating or self._active:
                    await self._condition.wait()
                self._updating = True
                owns_update = True
            except BaseException:
                if owns_update:
                    self._updating = False
                self._condition.notify_all()
                raise
            finally:
                self._waiting_updates -= 1

        try:
            yield
        finally:
            async with self._condition:
                self._updating = False
                self._condition.notify_all()

    async def status(self) -> dict[str, int | bool]:
        async with self._condition:
            return {
                "active": self._active,
                "updating": self._updating,
                "waiting_updates": self._waiting_updates,
            }

    async def _release(self) -> None:
        async with self._condition:
            if self._active <= 0:
                raise RuntimeError("configuration gate lease count is invalid")
            self._active -= 1
            if self._active == 0:
                self._condition.notify_all()
