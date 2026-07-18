from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path


class ReloadError(RuntimeError):
    """A sanitized credential reload failure."""


@dataclass(frozen=True, slots=True)
class ReloadResult:
    applied: bool
    changed: bool
    generation: int
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ReloadStatus:
    generation: int
    last_attempt_at: float | None
    last_success_at: float | None
    error: str | None


class CredentialFileReloader:
    """Poll a credential file and atomically apply changed byte payloads."""

    def __init__(
        self,
        path: str | Path,
        apply_payload: Callable[[bytes], Awaitable[None]],
        *,
        interval_seconds: float = 30.0,
        max_bytes: int = 1024 * 1024,
        wall_time: Callable[[], float] = time.time,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("reload interval must be positive")
        if max_bytes < 1:
            raise ValueError("reload size limit must be positive")
        self._path = Path(path)
        self._apply_payload = apply_payload
        self._interval_seconds = interval_seconds
        self._max_bytes = max_bytes
        self._wall_time = wall_time
        self._lock = asyncio.Lock()
        self._digest: bytes | None = None
        self._generation = 0
        self._last_attempt_at: float | None = None
        self._last_success_at: float | None = None
        self._error: str | None = None

    async def prime(self) -> None:
        """Mark the current file as the already-applied startup generation."""
        payload = await self._read_payload()
        async with self._lock:
            self._digest = self._hash(payload)
            self._generation = max(self._generation, 1)
            now = self._wall_time()
            self._last_attempt_at = now
            self._last_success_at = now
            self._error = None

    async def reload(self, *, force: bool = False) -> ReloadResult:
        async with self._lock:
            self._last_attempt_at = self._wall_time()
            try:
                payload = await self._read_payload()
            except ReloadError as exc:
                self._error = str(exc)
                return ReloadResult(
                    applied=False,
                    changed=False,
                    generation=self._generation,
                    error=self._error,
                )

            digest = self._hash(payload)
            changed = digest != self._digest
            if not force and not changed:
                self._error = None
                return ReloadResult(
                    applied=False,
                    changed=False,
                    generation=self._generation,
                )

            try:
                await self._apply_payload(payload)
            except Exception:
                self._error = "apply_failed"
                return ReloadResult(
                    applied=False,
                    changed=changed,
                    generation=self._generation,
                    error=self._error,
                )

            self._digest = digest
            self._generation += 1
            self._last_success_at = self._wall_time()
            self._error = None
            return ReloadResult(
                applied=True,
                changed=changed,
                generation=self._generation,
            )

    async def run(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(
                    stop_event.wait(), timeout=self._interval_seconds
                )
            except TimeoutError:
                await self.reload()

    async def status(self) -> ReloadStatus:
        async with self._lock:
            return ReloadStatus(
                generation=self._generation,
                last_attempt_at=self._last_attempt_at,
                last_success_at=self._last_success_at,
                error=self._error,
            )

    async def _read_payload(self) -> bytes:
        try:
            stat = await asyncio.to_thread(self._path.stat)
            if stat.st_size > self._max_bytes:
                raise ReloadError("file_too_large")
            payload = await asyncio.to_thread(self._path.read_bytes)
        except ReloadError:
            raise
        except OSError:
            raise ReloadError("file_unavailable") from None
        if not payload:
            raise ReloadError("file_empty")
        if len(payload) > self._max_bytes:
            raise ReloadError("file_too_large")
        return payload

    @staticmethod
    def _hash(payload: bytes) -> bytes:
        return hashlib.sha256(payload).digest()
