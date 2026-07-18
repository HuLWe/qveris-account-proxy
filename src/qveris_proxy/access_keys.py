from __future__ import annotations

import asyncio
import hashlib
import math
import secrets
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Literal, Protocol


PROXY_ACCESS_KEY_PREFIX = "sk-"
PROXY_ACCESS_KEY_TOKEN_BYTES = 32
PROXY_ACCESS_KEY_NAME_MAX_LENGTH = 64
PROXY_ACCESS_KEY_REQUEST_LIMIT_MAX = 1_000_000_000_000
PROXY_ACCESS_KEY_RPM_MAX = 1_000_000
PROXY_ACCESS_KEY_CONCURRENCY_MAX = 1024
PROXY_ACCESS_KEY_EXPIRES_AT_MAX = 253_402_300_799

ProxyAccessKeyKind = Literal["primary", "managed"]
ProxyAccessKeyRejectionReason = Literal[
    "invalid",
    "disabled",
    "expired",
    "request_limit",
    "rate_limit",
    "concurrency",
]


class ProxyAccessKeyError(RuntimeError):
    code = "proxy_access_key_error"


class ProxyAccessKeyNotFound(ProxyAccessKeyError):
    code = "proxy_access_key_not_found"


class PrimaryProxyAccessKeyRequired(ProxyAccessKeyError):
    code = "primary_proxy_access_key_required"


class ProxyAccessKeyRejected(ProxyAccessKeyError):
    code = "proxy_access_key_rejected"

    def __init__(
        self,
        reason: ProxyAccessKeyRejectionReason,
        *,
        retry_after: float | None = None,
        key_id: str | None = None,
    ) -> None:
        super().__init__(f"proxy access key rejected: {reason}")
        self.reason = reason
        self.retry_after = (
            max(1, math.ceil(retry_after)) if retry_after is not None else None
        )
        self.key_id = key_id


@dataclass(frozen=True, slots=True)
class ProxyAccessKeyRecord:
    id: str
    kind: ProxyAccessKeyKind
    name: str
    prefix: str
    suffix: str
    enabled: bool
    request_limit: int | None
    requests_used: int
    requests_per_minute: int | None
    max_concurrency: int
    expires_at: float | None
    created_at: float
    updated_at: float
    last_used_at: float | None
    active_requests: int = 0


@dataclass(frozen=True, slots=True)
class CreatedProxyAccessKey:
    key: ProxyAccessKeyRecord
    secret: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class ProxyAccessKeyConsumeResult:
    key: ProxyAccessKeyRecord | None
    reason: ProxyAccessKeyRejectionReason | None = None
    retry_after: float | None = None

    @property
    def accepted(self) -> bool:
        return self.key is not None and self.reason is None


class ProxyAccessKeyStore(Protocol):
    async def ensure_primary_proxy_access_key(
        self,
        *,
        secret_hash: str,
        prefix: str,
        suffix: str,
        max_concurrency: int,
    ) -> ProxyAccessKeyRecord: ...

    async def create_proxy_access_key(
        self,
        *,
        key_id: str,
        name: str,
        secret_hash: str,
        prefix: str,
        suffix: str,
        enabled: bool,
        request_limit: int | None,
        requests_per_minute: int | None,
        max_concurrency: int,
        expires_at: float | None,
    ) -> ProxyAccessKeyRecord: ...

    async def list_proxy_access_keys(self) -> list[ProxyAccessKeyRecord]: ...

    async def get_proxy_access_key(
        self, key_id: str
    ) -> ProxyAccessKeyRecord | None: ...

    async def get_proxy_access_key_by_hash(
        self, secret_hash: str
    ) -> ProxyAccessKeyRecord | None: ...

    async def inspect_proxy_access_key(
        self, secret_hash: str
    ) -> ProxyAccessKeyConsumeResult: ...

    async def update_proxy_access_key(
        self,
        key_id: str,
        *,
        name: str,
        enabled: bool,
        request_limit: int | None,
        requests_per_minute: int | None,
        max_concurrency: int,
        expires_at: float | None,
    ) -> ProxyAccessKeyRecord | None: ...

    async def delete_proxy_access_key(self, key_id: str) -> bool: ...

    async def reset_proxy_access_key_usage(
        self, key_id: str
    ) -> ProxyAccessKeyRecord | None: ...

    async def consume_proxy_access_key(
        self, secret_hash: str
    ) -> ProxyAccessKeyConsumeResult: ...


class _Unset:
    __slots__ = ()


UNSET = _Unset()


def generate_proxy_access_key() -> str:
    return PROXY_ACCESS_KEY_PREFIX + secrets.token_urlsafe(PROXY_ACCESS_KEY_TOKEN_BYTES)


def hash_proxy_access_key(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def proxy_access_key_parts(secret: str) -> tuple[str, str]:
    prefix = (
        PROXY_ACCESS_KEY_PREFIX
        if secret.startswith(PROXY_ACCESS_KEY_PREFIX)
        else secret[:4]
    )
    return prefix, secret[-4:] if len(secret) >= 4 else ""


@dataclass(slots=True)
class ProxyAccessKeyLease:
    key: ProxyAccessKeyRecord
    _manager: ProxyAccessKeyManager = field(repr=False)
    _release_task: asyncio.Task[None] | None = field(
        default=None, init=False, repr=False
    )

    @property
    def key_id(self) -> str:
        return self.key.id

    async def release(self) -> None:
        if self._release_task is None:
            self._release_task = asyncio.create_task(
                self._manager._release(self.key.id)
            )
        await asyncio.shield(self._release_task)


class ProxyAccessKeyManager:
    def __init__(
        self,
        store: ProxyAccessKeyStore,
        *,
        secret_factory: Callable[[], str] = generate_proxy_access_key,
        id_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
    ) -> None:
        self._store = store
        self._secret_factory = secret_factory
        self._id_factory = id_factory
        self._lock = asyncio.Lock()
        self._active_requests: dict[str, int] = {}

    async def initialize(
        self, legacy_token: str, *, max_concurrency: int = 256
    ) -> ProxyAccessKeyRecord:
        self._validate_max_concurrency(max_concurrency)
        prefix, suffix = proxy_access_key_parts(legacy_token)
        async with self._lock:
            key = await self._store.ensure_primary_proxy_access_key(
                secret_hash=hash_proxy_access_key(legacy_token),
                prefix=prefix,
                suffix=suffix,
                max_concurrency=max_concurrency,
            )
            return self._with_active_requests(key)

    async def create(
        self,
        name: str,
        *,
        request_limit: int | None = None,
        requests_per_minute: int | None = None,
        max_concurrency: int = 8,
        expires_at: float | None = None,
        enabled: bool = True,
    ) -> CreatedProxyAccessKey:
        normalized_name = self._validate_name(name)
        self._validate_optional_positive_int("request_limit", request_limit)
        self._validate_optional_positive_int("requests_per_minute", requests_per_minute)
        self._validate_max_concurrency(max_concurrency)
        self._validate_expiry(expires_at)
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a boolean")

        async with self._lock:
            for _ in range(3):
                secret = self._secret_factory()
                self._validate_generated_secret(secret)
                prefix, suffix = proxy_access_key_parts(secret)
                try:
                    key = await self._store.create_proxy_access_key(
                        key_id=self._id_factory(),
                        name=normalized_name,
                        secret_hash=hash_proxy_access_key(secret),
                        prefix=prefix,
                        suffix=suffix,
                        enabled=enabled,
                        request_limit=request_limit,
                        requests_per_minute=requests_per_minute,
                        max_concurrency=max_concurrency,
                        expires_at=expires_at,
                    )
                except ValueError:
                    continue
                return CreatedProxyAccessKey(
                    key=self._with_active_requests(key), secret=secret
                )
        raise RuntimeError("proxy access key generation collision")

    async def list(self) -> list[ProxyAccessKeyRecord]:
        async with self._lock:
            return [
                self._with_active_requests(key)
                for key in await self._store.list_proxy_access_keys()
            ]

    async def get(self, key_id: str) -> ProxyAccessKeyRecord:
        async with self._lock:
            key = await self._store.get_proxy_access_key(key_id)
            if key is None:
                raise ProxyAccessKeyNotFound("proxy access key not found")
            return self._with_active_requests(key)

    async def update(
        self,
        key_id: str,
        *,
        name: str | _Unset = UNSET,
        enabled: bool | _Unset = UNSET,
        request_limit: int | None | _Unset = UNSET,
        requests_per_minute: int | None | _Unset = UNSET,
        max_concurrency: int | _Unset = UNSET,
        expires_at: float | None | _Unset = UNSET,
    ) -> ProxyAccessKeyRecord:
        async with self._lock:
            current = await self._store.get_proxy_access_key(key_id)
            if current is None:
                raise ProxyAccessKeyNotFound("proxy access key not found")

            next_name = (
                current.name if isinstance(name, _Unset) else self._validate_name(name)
            )
            next_enabled = current.enabled if isinstance(enabled, _Unset) else enabled
            if not isinstance(next_enabled, bool):
                raise ValueError("enabled must be a boolean")
            next_request_limit = (
                current.request_limit
                if isinstance(request_limit, _Unset)
                else request_limit
            )
            next_rpm = (
                current.requests_per_minute
                if isinstance(requests_per_minute, _Unset)
                else requests_per_minute
            )
            next_concurrency = (
                current.max_concurrency
                if isinstance(max_concurrency, _Unset)
                else max_concurrency
            )
            next_expiry = (
                current.expires_at if isinstance(expires_at, _Unset) else expires_at
            )
            self._validate_optional_positive_int("request_limit", next_request_limit)
            self._validate_optional_positive_int("requests_per_minute", next_rpm)
            self._validate_max_concurrency(next_concurrency)
            self._validate_expiry(next_expiry)

            key = await self._store.update_proxy_access_key(
                key_id,
                name=next_name,
                enabled=next_enabled,
                request_limit=next_request_limit,
                requests_per_minute=next_rpm,
                max_concurrency=next_concurrency,
                expires_at=next_expiry,
            )
            if key is None:
                raise ProxyAccessKeyNotFound("proxy access key not found")
            return self._with_active_requests(key)

    async def delete(self, key_id: str) -> None:
        async with self._lock:
            current = await self._store.get_proxy_access_key(key_id)
            if current is None:
                raise ProxyAccessKeyNotFound("proxy access key not found")
            if current.kind == "primary":
                raise PrimaryProxyAccessKeyRequired(
                    "primary proxy access key is required"
                )
            if not await self._store.delete_proxy_access_key(key_id):
                raise ProxyAccessKeyNotFound("proxy access key not found")
            self._active_requests.pop(key_id, None)

    async def reset_usage(self, key_id: str) -> ProxyAccessKeyRecord:
        async with self._lock:
            key = await self._store.reset_proxy_access_key_usage(key_id)
            if key is None:
                raise ProxyAccessKeyNotFound("proxy access key not found")
            return self._with_active_requests(key)

    async def acquire(self, candidate: str) -> ProxyAccessKeyLease:
        secret_hash = hash_proxy_access_key(candidate)
        async with self._lock:
            eligibility = await self._store.inspect_proxy_access_key(secret_hash)
            if not eligibility.accepted:
                assert eligibility.reason is not None
                raise ProxyAccessKeyRejected(
                    eligibility.reason,
                    retry_after=eligibility.retry_after,
                    key_id=(
                        eligibility.key.id if eligibility.key is not None else None
                    ),
                )
            assert eligibility.key is not None
            key = eligibility.key
            active_requests = self._active_requests.get(key.id, 0)
            if active_requests >= key.max_concurrency:
                raise ProxyAccessKeyRejected(
                    "concurrency", retry_after=1, key_id=key.id
                )

            result = await self._store.consume_proxy_access_key(secret_hash)
            if not result.accepted:
                assert result.reason is not None
                raise ProxyAccessKeyRejected(
                    result.reason,
                    retry_after=result.retry_after,
                    key_id=key.id,
                )
            assert result.key is not None
            active_requests += 1
            self._active_requests[key.id] = active_requests
            acquired = replace(result.key, active_requests=active_requests)
            return ProxyAccessKeyLease(key=acquired, _manager=self)

    async def _release(self, key_id: str) -> None:
        async with self._lock:
            active_requests = self._active_requests.get(key_id, 0)
            if active_requests <= 1:
                self._active_requests.pop(key_id, None)
            else:
                self._active_requests[key_id] = active_requests - 1

    def _with_active_requests(self, key: ProxyAccessKeyRecord) -> ProxyAccessKeyRecord:
        return replace(key, active_requests=self._active_requests.get(key.id, 0))

    @staticmethod
    def _validate_name(name: str) -> str:
        if not isinstance(name, str):
            raise ValueError("name must be a string")
        normalized = name.strip()
        if not normalized or len(normalized) > PROXY_ACCESS_KEY_NAME_MAX_LENGTH:
            raise ValueError(
                "name must contain between 1 and "
                f"{PROXY_ACCESS_KEY_NAME_MAX_LENGTH} characters"
            )
        return normalized

    @staticmethod
    def _validate_optional_positive_int(name: str, value: int | None) -> None:
        maximum = {
            "request_limit": PROXY_ACCESS_KEY_REQUEST_LIMIT_MAX,
            "requests_per_minute": PROXY_ACCESS_KEY_RPM_MAX,
        }[name]
        if value is not None and (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 1 <= value <= maximum
        ):
            raise ValueError(f"{name} must be between 1 and {maximum} or null")

    @staticmethod
    def _validate_max_concurrency(value: int) -> None:
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 1 <= value <= PROXY_ACCESS_KEY_CONCURRENCY_MAX
        ):
            raise ValueError(
                "max_concurrency must be between 1 and "
                f"{PROXY_ACCESS_KEY_CONCURRENCY_MAX}"
            )

    @staticmethod
    def _validate_expiry(value: float | None) -> None:
        if value is not None and (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
            or value > PROXY_ACCESS_KEY_EXPIRES_AT_MAX
        ):
            raise ValueError("expires_at must be a positive Unix timestamp or null")

    @staticmethod
    def _validate_generated_secret(secret: str) -> None:
        token = secret.removeprefix(PROXY_ACCESS_KEY_PREFIX)
        if (
            not secret.startswith(PROXY_ACCESS_KEY_PREFIX)
            or len(token) != 43
            or any(
                character
                not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
                for character in token
            )
        ):
            raise ValueError("generated proxy access key has an invalid format")
