from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import threading
import time
from collections.abc import Callable

from pydantic import BaseModel, ConfigDict, Field


BOOTSTRAP_TICKET_TTL_SECONDS = 60
BOOTSTRAP_EXCHANGE_MAX_BYTES = 512
ADMIN_BROWSER_SESSION_MAX_AGE_SECONDS = 180 * 24 * 60 * 60
ADMIN_BROWSER_SESSION_COOKIE = "qveris_admin_session"
ADMIN_BROWSER_SESSION_HEADER = "x-qveris-admin-session"
_TICKET_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")
_SESSION_NONCE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{22}$")
_SESSION_SIGNATURE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")


class BootstrapExchangeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticket: str = Field(min_length=43, max_length=43, pattern=_TICKET_PATTERN.pattern)


class BootstrapTicketCapacityError(RuntimeError):
    pass


class AdminBrowserSessions:
    def __init__(
        self,
        access_token: str,
        *,
        max_age_seconds: int = ADMIN_BROWSER_SESSION_MAX_AGE_SECONDS,
        wall_time: Callable[[], float] = time.time,
    ) -> None:
        if not access_token or max_age_seconds < 1:
            raise ValueError("invalid admin browser session settings")
        self._max_age_seconds = max_age_seconds
        self._wall_time = wall_time
        self._signing_key = hmac.new(
            access_token.encode("utf-8"),
            b"qveris-proxy-admin-browser-session-v1",
            hashlib.sha256,
        ).digest()

    @property
    def max_age_seconds(self) -> int:
        return self._max_age_seconds

    @property
    def claim_key(self) -> str:
        return hmac.new(
            self._signing_key,
            b"first-open-claim-v1",
            hashlib.sha256,
        ).hexdigest()

    def issue(self) -> str:
        expires_at = int(self._wall_time()) + self._max_age_seconds
        nonce = secrets.token_urlsafe(16)
        payload = f"v1.{expires_at}.{nonce}"
        return f"{payload}.{self._signature(payload)}"

    def validate(self, value: str) -> bool:
        parts = value.split(".")
        if len(parts) != 4:
            return False
        version, raw_expires_at, nonce, candidate_signature = parts
        if (
            version != "v1"
            or not raw_expires_at.isascii()
            or not raw_expires_at.isdecimal()
            or _SESSION_NONCE_PATTERN.fullmatch(nonce) is None
            or _SESSION_SIGNATURE_PATTERN.fullmatch(candidate_signature) is None
        ):
            return False
        expires_at = int(raw_expires_at)
        now = int(self._wall_time())
        if expires_at <= now or expires_at > now + self._max_age_seconds:
            return False
        payload = f"{version}.{raw_expires_at}.{nonce}"
        return hmac.compare_digest(candidate_signature, self._signature(payload))

    def _signature(self, payload: str) -> str:
        digest = hmac.new(
            self._signing_key,
            payload.encode("ascii"),
            hashlib.sha256,
        ).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


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
