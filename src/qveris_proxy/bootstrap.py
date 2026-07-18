from __future__ import annotations

import hashlib
import re
import secrets
import threading
import time
from collections.abc import Callable

from pydantic import BaseModel, ConfigDict, Field


BOOTSTRAP_TICKET_TTL_SECONDS = 60
BOOTSTRAP_EXCHANGE_MAX_BYTES = 512
_TICKET_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")


class BootstrapExchangeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticket: str = Field(min_length=43, max_length=43, pattern=_TICKET_PATTERN.pattern)


class BootstrapTicketCapacityError(RuntimeError):
    pass


class AdminBootstrapTickets:
    def __init__(
        self,
        *,
        ttl_seconds: float = BOOTSTRAP_TICKET_TTL_SECONDS,
        max_pending: int = 32,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl_seconds <= 0 or max_pending < 1:
            raise ValueError("invalid bootstrap ticket limits")
        self._ttl_seconds = ttl_seconds
        self._max_pending = max_pending
        self._clock = clock
        self._tickets: dict[bytes, float] = {}
        self._lock = threading.Lock()

    def issue(self) -> str:
        ticket = secrets.token_urlsafe(32)
        digest = self._digest(ticket)
        now = self._clock()
        with self._lock:
            self._prune(now)
            if len(self._tickets) >= self._max_pending:
                raise BootstrapTicketCapacityError("bootstrap ticket capacity reached")
            self._tickets[digest] = now + self._ttl_seconds
        return ticket

    def consume(self, ticket: str) -> bool:
        if _TICKET_PATTERN.fullmatch(ticket) is None:
            return False
        digest = self._digest(ticket)
        now = self._clock()
        with self._lock:
            expires_at = self._tickets.pop(digest, None)
            self._prune(now)
        return expires_at is not None and expires_at > now

    def clear(self) -> None:
        with self._lock:
            self._tickets.clear()

    @staticmethod
    def _digest(ticket: str) -> bytes:
        return hashlib.sha256(ticket.encode("ascii")).digest()

    def _prune(self, now: float) -> None:
        expired = [
            digest for digest, expires_at in self._tickets.items() if expires_at <= now
        ]
        for digest in expired:
            del self._tickets[digest]
