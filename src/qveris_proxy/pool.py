from __future__ import annotations

import asyncio
import hashlib
import math
import random
import time
from dataclasses import dataclass, field
from datetime import timezone
from email.utils import parsedate_to_datetime
from typing import Callable, Literal

from pydantic import SecretStr

from .config import AccountConfig, ProxySettings
from .state import StoredCooldown

CredentialKind = Literal["api_key", "oauth"]
_CONTROL_REQUESTS_PER_MINUTE = 2.0
_CONTROL_BURST = 4


class PoolUnavailable(RuntimeError):
    def __init__(self, retry_after: float = 1.0, *, reason: str = "capacity") -> None:
        super().__init__("account is temporarily unavailable")
        self.retry_after = max(1, math.ceil(retry_after))
        self.reason = reason


@dataclass(slots=True)
class _KeyState:
    id: str
    api_key: SecretStr = field(repr=False)
    inflight: int = 0
    cooldown_until: float = 0.0


@dataclass(slots=True)
class _AccountState:
    id: str
    api_keys: list[_KeyState]
    oauth_tokens: list[_KeyState]
    cursors: dict[CredentialKind, int] = field(default_factory=dict)
    route_cooldowns: dict[str, float] = field(default_factory=dict)
    credit_cooldown_until: float = 0.0
    forbidden_cooldown_until: float = 0.0
    failure_cooldown_until: float = 0.0
    failure_count: int = 0
    credit_depleted: bool = False
    weight: int = 1
    requests_per_minute: float = 10.0
    burst: int = 10
    tokens: float = 10.0
    token_updated_at: float = 0.0
    control_tokens: float = float(_CONTROL_BURST)
    control_token_updated_at: float = 0.0


@dataclass(frozen=True, slots=True)
class _CredentialRuntime:
    id: str
    fingerprint: bytes = field(repr=False)
    cooldown_remaining: float = 0.0


@dataclass(frozen=True, slots=True)
class _AccountRuntime:
    api_keys: tuple[_CredentialRuntime, ...] = field(repr=False)
    oauth_tokens: tuple[_CredentialRuntime, ...] = field(repr=False)
    cursors: dict[CredentialKind, int]
    route_cooldowns: dict[str, float]
    credit_cooldown_remaining: float
    forbidden_cooldown_remaining: float
    failure_cooldown_remaining: float
    failure_count: int
    credit_depleted: bool
    tokens: float
    control_tokens: float


@dataclass(frozen=True, slots=True)
class _PoolRuntime:
    accounts: dict[str, _AccountRuntime]
    account_schedule: tuple[str, ...]
    account_cursor: int


@dataclass(slots=True)
class KeyLease:
    account_id: str
    key_id: str
    route_id: str
    credential_kind: CredentialKind
    credit_sensitive: bool
    _token: SecretStr = field(repr=False)
    _pool: KeyPool = field(repr=False)
    fallback_for_credential_kind: CredentialKind | None = field(
        default=None, repr=False
    )
    _release_task: asyncio.Task[None] | None = field(
        default=None, init=False, repr=False
    )

    @property
    def bearer_token(self) -> str:
        return self._token.get_secret_value()

    @property
    def api_key(self) -> str:
        return self.bearer_token

    async def release(self) -> None:
        if self._release_task is None:
            self._release_task = asyncio.create_task(
                self._pool._release(
                    self.account_id,
                    self.credential_kind,
                    self.key_id,
                )
            )
        await asyncio.shield(self._release_task)

    async def report_response(
        self, status_code: int, headers: dict[str, str]
    ) -> StoredCooldown | None:
        return await self._pool.report_response(self, status_code, headers)

    async def report_transport_failure(self) -> StoredCooldown:
        return await self._pool.report_transport_failure(self)

    async def report_credit_balance(self, credits: float) -> StoredCooldown | None:
        return await self._pool.report_credit_balance(self.account_id, credits)


class KeyPool:
    def __init__(
        self,
        settings: ProxySettings,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        wall_time: Callable[[], float] = time.time,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self._settings = settings
        self._monotonic = monotonic
        self._wall_time = wall_time
        self._jitter = jitter
        self._lock = asyncio.Lock()
        now = monotonic()
        self._accounts = {
            account.id: self._build_account(account, now)
            for account in settings.accounts
        }
        self._account_schedule = [
            account.id for account in settings.accounts for _ in range(account.weight)
        ]
        self._account_cursor = 0

    @staticmethod
    def _build_account(account: AccountConfig, now: float) -> _AccountState:
        return _AccountState(
            id=account.id,
            weight=account.weight,
            requests_per_minute=account.requests_per_minute,
            burst=account.burst,
            tokens=float(account.burst),
            token_updated_at=now,
            control_tokens=float(_CONTROL_BURST),
            control_token_updated_at=now,
            api_keys=[
                _KeyState(id=item.id, api_key=item.api_key) for item in account.keys
            ],
            oauth_tokens=[
                _KeyState(id=item.id, api_key=item.access_token)
                for item in account.oauth_tokens
            ],
        )

    def has_account(self, account_id: str) -> bool:
        return account_id in self._accounts

    def account_ids(self) -> tuple[str, ...]:
        return tuple(self._accounts)

    async def migrate_runtime_from(self, previous: KeyPool) -> None:
        """Copy live state for retained accounts without carrying active leases."""
        if previous is self:
            return

        runtime = await previous._runtime_snapshot()
        async with self._lock:
            now = self._monotonic()
            self._account_cursor = self._migrate_schedule_cursor(
                runtime.account_schedule,
                runtime.account_cursor,
                tuple(self._account_schedule),
            )
            for account_id, previous_account in runtime.accounts.items():
                account = self._accounts.get(account_id)
                if account is None:
                    continue

                account.tokens = min(
                    float(account.burst), max(0.0, previous_account.tokens)
                )
                account.token_updated_at = now
                account.control_tokens = min(
                    float(_CONTROL_BURST),
                    max(0.0, previous_account.control_tokens),
                )
                account.control_token_updated_at = now
                account.route_cooldowns = {
                    route_id: now + remaining
                    for route_id, remaining in previous_account.route_cooldowns.items()
                    if remaining > 0
                }
                account.credit_cooldown_until = (
                    now + previous_account.credit_cooldown_remaining
                    if previous_account.credit_cooldown_remaining > 0
                    else 0.0
                )
                account.forbidden_cooldown_until = (
                    now + previous_account.forbidden_cooldown_remaining
                    if previous_account.forbidden_cooldown_remaining > 0
                    else 0.0
                )
                account.failure_cooldown_until = (
                    now + previous_account.failure_cooldown_remaining
                    if previous_account.failure_cooldown_remaining > 0
                    else 0.0
                )
                account.failure_count = previous_account.failure_count
                account.credit_depleted = previous_account.credit_depleted

                self._migrate_credentials_locked(
                    account,
                    "api_key",
                    previous_account.api_keys,
                    previous_account.cursors.get("api_key", 0),
                    now,
                )
                self._migrate_credentials_locked(
                    account,
                    "oauth",
                    previous_account.oauth_tokens,
                    previous_account.cursors.get("oauth", 0),
                    now,
                )

    async def acquire_any(
        self,
        route_id: str,
        credential_kind: CredentialKind = "api_key",
        *,
        credit_sensitive: bool = False,
        control: bool = False,
        fallback_credential_kind: CredentialKind | None = None,
    ) -> KeyLease:
        async with self._lock:
            schedule_size = len(self._account_schedule)
            failures: list[tuple[str, float]] = []
            for offset in range(schedule_size):
                index = (self._account_cursor + offset) % schedule_size
                account_id = self._account_schedule[index]
                try:
                    lease = self._acquire_locked_with_fallback(
                        account_id,
                        route_id,
                        credential_kind,
                        credit_sensitive=credit_sensitive,
                        control=control,
                        fallback_credential_kind=fallback_credential_kind,
                    )
                except PoolUnavailable as exc:
                    failures.append((exc.reason, float(exc.retry_after)))
                    continue
                self._account_cursor = (index + 1) % schedule_size
                return lease
            eligible_failures = [
                failure for failure in failures if failure[0] != "missing_credentials"
            ]
            retry_after = min(
                (retry for _, retry in eligible_failures or failures),
                default=1.0,
            )
            eligible_reasons = [reason for reason, _ in eligible_failures]
            if not eligible_reasons:
                reason = "missing_credentials"
            elif all(item == "credits" for item in eligible_reasons):
                reason = "credits"
            elif all(item == "rate_limit" for item in eligible_reasons):
                reason = "rate_limit"
            elif all(item == "forbidden" for item in eligible_reasons):
                reason = "forbidden"
            elif all(item == "upstream" for item in eligible_reasons):
                reason = "upstream"
            else:
                reason = "capacity"
            raise PoolUnavailable(retry_after, reason=reason)

    async def acquire(
        self,
        account_id: str,
        route_id: str,
        credential_kind: CredentialKind = "api_key",
        *,
        credit_sensitive: bool = False,
        control: bool = False,
        fallback_credential_kind: CredentialKind | None = None,
    ) -> KeyLease:
        async with self._lock:
            return self._acquire_locked_with_fallback(
                account_id,
                route_id,
                credential_kind,
                credit_sensitive=credit_sensitive,
                control=control,
                fallback_credential_kind=fallback_credential_kind,
            )

    def _acquire_locked_with_fallback(
        self,
        account_id: str,
        route_id: str,
        credential_kind: CredentialKind,
        *,
        credit_sensitive: bool,
        control: bool,
        fallback_credential_kind: CredentialKind | None,
    ) -> KeyLease:
        try:
            return self._acquire_locked(
                account_id,
                route_id,
                credential_kind,
                credit_sensitive=credit_sensitive,
                control=control,
            )
        except PoolUnavailable as exc:
            if (
                fallback_credential_kind is None
                or fallback_credential_kind == credential_kind
                or exc.reason != "missing_credentials"
            ):
                raise
            lease = self._acquire_locked(
                account_id,
                route_id,
                fallback_credential_kind,
                credit_sensitive=credit_sensitive,
                control=control,
            )
            lease.fallback_for_credential_kind = credential_kind
            return lease

    def _acquire_locked(
        self,
        account_id: str,
        route_id: str,
        credential_kind: CredentialKind,
        *,
        credit_sensitive: bool,
        control: bool,
    ) -> KeyLease:
        account = self._accounts.get(account_id)
        if account is None:
            raise KeyError("unknown account")

        now = self._monotonic()
        if account.forbidden_cooldown_until > now:
            raise PoolUnavailable(
                account.forbidden_cooldown_until - now, reason="forbidden"
            )
        if account.failure_cooldown_until > now:
            raise PoolUnavailable(
                account.failure_cooldown_until - now, reason="upstream"
            )
        if credit_sensitive and account.credit_depleted:
            retry_after = (
                self._settings.quota_refresh_interval_seconds
                or self._settings.payment_required_cooldown_seconds
            )
            raise PoolUnavailable(retry_after, reason="credits")
        if credit_sensitive and account.credit_cooldown_until > now:
            raise PoolUnavailable(account.credit_cooldown_until - now, reason="credits")
        route_cooldown = account.route_cooldowns.get(route_id, 0.0)
        if route_cooldown > now:
            raise PoolUnavailable(route_cooldown - now, reason="rate_limit")

        credentials = self._credentials(account, credential_kind)
        key_count = len(credentials)
        if key_count == 0:
            raise PoolUnavailable(1.0, reason="missing_credentials")
        cursor = account.cursors.get(credential_kind, 0)
        selected: tuple[int, _KeyState] | None = None
        for offset in range(key_count):
            index = (cursor + offset) % key_count
            key = credentials[index]
            if key.cooldown_until > now:
                continue
            if key.inflight >= self._settings.max_inflight_per_key:
                continue
            selected = (index, key)
            break

        if selected is not None:
            rate_limit_wait = (
                self._control_rate_limit_wait_locked(account, now)
                if control
                else self._rate_limit_wait_locked(account, now)
            )
            if rate_limit_wait > 0:
                raise PoolUnavailable(rate_limit_wait, reason="rate_limit")
            index, key = selected
            if control:
                account.control_tokens -= 1.0
            else:
                account.tokens -= 1.0
            key.inflight += 1
            account.cursors[credential_kind] = (index + 1) % key_count
            return KeyLease(
                account_id=account.id,
                key_id=key.id,
                route_id=route_id,
                credential_kind=credential_kind,
                credit_sensitive=credit_sensitive,
                _token=key.api_key,
                _pool=self,
            )

        cooldowns = [
            key.cooldown_until - now for key in credentials if key.cooldown_until > now
        ]
        retry_after = min(cooldowns) if cooldowns else 1.0
        reason = "credentials" if len(cooldowns) == key_count else "capacity"
        raise PoolUnavailable(retry_after, reason=reason)

    async def report_response(
        self, lease: KeyLease, status_code: int, headers: dict[str, str]
    ) -> StoredCooldown | None:
        async with self._lock:
            account = self._accounts[lease.account_id]
            now = self._monotonic()
            if (
                lease.fallback_for_credential_kind is not None
                and (status_code in {401, 402, 403} or status_code >= 500)
            ):
                return None
            if status_code >= 500:
                return self._record_failure_locked(account, now)

            cleared_failure = self._clear_failure_locked(account)
            clears = (("health", "upstream"),) if cleared_failure else ()
            if status_code == 429:
                cooldown = self._parse_retry_after(
                    headers,
                    default=self._settings.max_rate_limit_cooldown_seconds,
                )
                account.route_cooldowns[lease.route_id] = max(
                    account.route_cooldowns.get(lease.route_id, 0.0),
                    now + cooldown,
                )
                return StoredCooldown(
                    scope="route",
                    account_id=lease.account_id,
                    name=lease.route_id,
                    until_epoch=self._wall_time() + cooldown,
                    clears=clears,
                )
            if status_code == 403:
                cooldown = self._parse_retry_after(
                    headers,
                    default=self._settings.forbidden_cooldown_seconds,
                )
                account.forbidden_cooldown_until = max(
                    account.forbidden_cooldown_until,
                    now + cooldown,
                )
                return StoredCooldown(
                    scope="account",
                    account_id=lease.account_id,
                    name="forbidden",
                    until_epoch=self._wall_time() + cooldown,
                    clears=clears,
                )
            if status_code == 402:
                cooldown = self._settings.payment_required_cooldown_seconds
                account.credit_cooldown_until = max(
                    account.credit_cooldown_until,
                    now + cooldown,
                )
                return StoredCooldown(
                    scope="account",
                    account_id=lease.account_id,
                    name="credits",
                    until_epoch=self._wall_time() + cooldown,
                    clears=clears,
                )
            if status_code == 401:
                key = self._find_key(account, lease.credential_kind, lease.key_id)
                key.cooldown_until = max(
                    key.cooldown_until,
                    now + self._settings.auth_failure_cooldown_seconds,
                )
                return StoredCooldown(
                    scope=f"credential:{lease.credential_kind}",
                    account_id=lease.account_id,
                    name=self._credential_state_name(key),
                    until_epoch=(
                        self._wall_time() + self._settings.auth_failure_cooldown_seconds
                    ),
                    clears=clears,
                )
            if cleared_failure:
                return StoredCooldown(
                    scope="health",
                    account_id=lease.account_id,
                    name="upstream",
                    until_epoch=0.0,
                    delete=True,
                )
            return None

    async def report_transport_failure(self, lease: KeyLease) -> StoredCooldown:
        async with self._lock:
            account = self._accounts[lease.account_id]
            return self._record_failure_locked(account, self._monotonic())

    async def report_credit_balance(
        self, account_id: str, credits: float
    ) -> StoredCooldown | None:
        if not math.isfinite(credits):
            raise ValueError("credit balance must be finite")
        async with self._lock:
            account = self._accounts[account_id]
            if credits <= 0:
                if account.credit_depleted:
                    return None
                account.credit_depleted = True
                return StoredCooldown(
                    scope="health",
                    account_id=account_id,
                    name="depleted",
                    until_epoch=0.0,
                    retain_after_expiry=True,
                )

            changed = account.credit_depleted or account.credit_cooldown_until > 0
            account.credit_depleted = False
            account.credit_cooldown_until = 0.0
            if not changed:
                return None
            return StoredCooldown(
                scope="health",
                account_id=account_id,
                name="depleted",
                until_epoch=0.0,
                delete=True,
                clears=(("account", "credits"),),
            )

    async def restore_cooldowns(self, records: list[StoredCooldown]) -> None:
        async with self._lock:
            now_wall = self._wall_time()
            now_mono = self._monotonic()
            for record in records:
                remaining = record.until_epoch - now_wall
                account = self._accounts.get(record.account_id)
                if account is None:
                    continue
                if record.scope == "health" and record.name == "upstream":
                    account.failure_count = max(
                        account.failure_count, record.failure_count
                    )
                    if remaining > 0:
                        account.failure_cooldown_until = max(
                            account.failure_cooldown_until,
                            now_mono + remaining,
                        )
                    continue
                if record.scope == "health" and record.name == "depleted":
                    account.credit_depleted = True
                    continue
                if remaining <= 0:
                    continue
                until = now_mono + remaining
                if record.scope == "route":
                    account.route_cooldowns[record.name] = max(
                        account.route_cooldowns.get(record.name, 0.0), until
                    )
                elif record.scope == "account" and record.name == "credits":
                    account.credit_cooldown_until = max(
                        account.credit_cooldown_until, until
                    )
                elif record.scope == "account" and record.name == "forbidden":
                    account.forbidden_cooldown_until = max(
                        account.forbidden_cooldown_until, until
                    )
                elif record.scope in {
                    "key",
                    "credential:api_key",
                    "credential:oauth",
                }:
                    kind: CredentialKind = (
                        "oauth" if record.scope == "credential:oauth" else "api_key"
                    )
                    for key in self._credentials(account, kind):
                        if self._credential_state_name(key) == record.name:
                            key.cooldown_until = max(key.cooldown_until, until)
                            break

    async def status(self) -> list[dict[str, object]]:
        async with self._lock:
            now = self._monotonic()
            result: list[dict[str, object]] = []
            for account in self._accounts.values():
                self._refill_tokens_locked(account, now)
                self._refill_control_tokens_locked(account, now)
                result.append(
                    {
                        "id": account.id,
                        "weight": account.weight,
                        "available_keys": sum(
                            1 for key in account.api_keys if key.cooldown_until <= now
                        ),
                        "total_keys": len(account.api_keys),
                        "available_oauth_tokens": sum(
                            1
                            for token in account.oauth_tokens
                            if token.cooldown_until <= now
                        ),
                        "total_oauth_tokens": len(account.oauth_tokens),
                        "credit_cooldown": max(
                            0, math.ceil(account.credit_cooldown_until - now)
                        ),
                        "credit_depleted": account.credit_depleted,
                        "forbidden_cooldown": max(
                            0, math.ceil(account.forbidden_cooldown_until - now)
                        ),
                        "upstream_failure_count": account.failure_count,
                        "upstream_cooldown": max(
                            0, math.ceil(account.failure_cooldown_until - now)
                        ),
                        "rate_limit": {
                            "requests_per_minute": account.requests_per_minute,
                            "burst": account.burst,
                            "available_tokens": round(account.tokens, 3),
                            "control_requests_per_minute": (
                                _CONTROL_REQUESTS_PER_MINUTE
                            ),
                            "control_burst": _CONTROL_BURST,
                            "control_available_tokens": round(
                                account.control_tokens, 3
                            ),
                        },
                        "route_cooldowns": {
                            route: max(0, math.ceil(until - now))
                            for route, until in account.route_cooldowns.items()
                            if until > now
                        },
                    }
                )
            return result

    async def is_ready(self) -> bool:
        async with self._lock:
            now = self._monotonic()
            return any(
                account.forbidden_cooldown_until <= now
                and account.failure_cooldown_until <= now
                and any(
                    credential.cooldown_until <= now
                    for credential in (*account.api_keys, *account.oauth_tokens)
                )
                for account in self._accounts.values()
            )

    async def clear_credit_cooldown(self, account_id: str) -> bool:
        async with self._lock:
            account = self._accounts[account_id]
            if account.credit_cooldown_until == 0:
                return False
            account.credit_cooldown_until = 0
            return True

    async def _runtime_snapshot(self) -> _PoolRuntime:
        async with self._lock:
            now = self._monotonic()
            accounts: dict[str, _AccountRuntime] = {}
            for account in self._accounts.values():
                self._refill_tokens_locked(account, now)
                self._refill_control_tokens_locked(account, now)
                accounts[account.id] = _AccountRuntime(
                    api_keys=self._credential_runtime(account.api_keys, now),
                    oauth_tokens=self._credential_runtime(account.oauth_tokens, now),
                    cursors=dict(account.cursors),
                    route_cooldowns={
                        route_id: max(0.0, until - now)
                        for route_id, until in account.route_cooldowns.items()
                        if until > now
                    },
                    credit_cooldown_remaining=max(
                        0.0, account.credit_cooldown_until - now
                    ),
                    forbidden_cooldown_remaining=max(
                        0.0, account.forbidden_cooldown_until - now
                    ),
                    failure_cooldown_remaining=max(
                        0.0, account.failure_cooldown_until - now
                    ),
                    failure_count=account.failure_count,
                    credit_depleted=account.credit_depleted,
                    tokens=account.tokens,
                    control_tokens=account.control_tokens,
                )
            return _PoolRuntime(
                accounts=accounts,
                account_schedule=tuple(self._account_schedule),
                account_cursor=self._account_cursor,
            )

    @classmethod
    def _credential_runtime(
        cls, credentials: list[_KeyState], now: float
    ) -> tuple[_CredentialRuntime, ...]:
        return tuple(
            _CredentialRuntime(
                id=credential.id,
                fingerprint=cls._credential_fingerprint(credential),
                cooldown_remaining=max(0.0, credential.cooldown_until - now),
            )
            for credential in credentials
        )

    @classmethod
    def _migrate_credentials_locked(
        cls,
        account: _AccountState,
        credential_kind: CredentialKind,
        previous: tuple[_CredentialRuntime, ...],
        previous_cursor: int,
        now: float,
    ) -> None:
        current = cls._credentials(account, credential_kind)
        previous_by_identity = {
            (credential.id, credential.fingerprint): credential
            for credential in previous
        }
        for credential in current:
            identity = (
                credential.id,
                cls._credential_fingerprint(credential),
            )
            previous_credential = previous_by_identity.get(identity)
            credential.cooldown_until = (
                now + previous_credential.cooldown_remaining
                if previous_credential is not None
                and previous_credential.cooldown_remaining > 0
                else 0.0
            )

        if current:
            account.cursors[credential_kind] = cls._migrate_credential_cursor(
                previous,
                previous_cursor,
                current,
            )
        else:
            account.cursors.pop(credential_kind, None)

    @classmethod
    def _migrate_credential_cursor(
        cls,
        previous: tuple[_CredentialRuntime, ...],
        previous_cursor: int,
        current: list[_KeyState],
    ) -> int:
        if not current:
            return 0
        current_positions = {
            (credential.id, cls._credential_fingerprint(credential)): index
            for index, credential in enumerate(current)
        }
        if previous:
            for offset in range(len(previous)):
                credential = previous[(previous_cursor + offset) % len(previous)]
                position = current_positions.get(
                    (credential.id, credential.fingerprint)
                )
                if position is not None:
                    return position
        return previous_cursor % len(current)

    @staticmethod
    def _migrate_schedule_cursor(
        previous_schedule: tuple[str, ...],
        previous_cursor: int,
        current_schedule: tuple[str, ...],
    ) -> int:
        if not previous_schedule or not current_schedule:
            return 0
        for offset in range(len(previous_schedule)):
            previous_index = (previous_cursor + offset) % len(previous_schedule)
            account_id = previous_schedule[previous_index]
            current_positions = [
                index
                for index, candidate in enumerate(current_schedule)
                if candidate == account_id
            ]
            if current_positions:
                occurrence = sum(
                    candidate == account_id
                    for candidate in previous_schedule[:previous_index]
                )
                return current_positions[occurrence % len(current_positions)]
        return 0

    @staticmethod
    def _credential_fingerprint(credential: _KeyState) -> bytes:
        return hashlib.sha256(
            credential.api_key.get_secret_value().encode("utf-8")
        ).digest()

    @classmethod
    def _credential_state_name(cls, credential: _KeyState) -> str:
        return f"sha256:{cls._credential_fingerprint(credential).hex()}"

    @staticmethod
    def _refill_tokens_locked(account: _AccountState, now: float) -> None:
        elapsed = max(0.0, now - account.token_updated_at)
        account.tokens = min(
            float(account.burst),
            account.tokens + elapsed * account.requests_per_minute / 60.0,
        )
        account.token_updated_at = now

    def _rate_limit_wait_locked(self, account: _AccountState, now: float) -> float:
        self._refill_tokens_locked(account, now)
        if account.tokens >= 1.0:
            return 0.0
        refill_per_second = account.requests_per_minute / 60.0
        return (1.0 - account.tokens) / refill_per_second

    @staticmethod
    def _refill_control_tokens_locked(account: _AccountState, now: float) -> None:
        elapsed = max(0.0, now - account.control_token_updated_at)
        account.control_tokens = min(
            float(_CONTROL_BURST),
            account.control_tokens + elapsed * _CONTROL_REQUESTS_PER_MINUTE / 60.0,
        )
        account.control_token_updated_at = now

    @classmethod
    def _control_rate_limit_wait_locked(
        cls, account: _AccountState, now: float
    ) -> float:
        cls._refill_control_tokens_locked(account, now)
        if account.control_tokens >= 1.0:
            return 0.0
        refill_per_second = _CONTROL_REQUESTS_PER_MINUTE / 60.0
        return (1.0 - account.control_tokens) / refill_per_second

    def _record_failure_locked(
        self, account: _AccountState, now: float
    ) -> StoredCooldown:
        account.failure_count += 1
        exponent = min(account.failure_count - 1, 30)
        ceiling = min(
            self._settings.failure_backoff_max_seconds,
            self._settings.failure_backoff_base_seconds * (2**exponent),
        )
        jitter_value = min(1.0, max(0.0, self._jitter()))
        cooldown = ceiling * (0.75 + 0.25 * jitter_value)
        account.failure_cooldown_until = max(
            account.failure_cooldown_until,
            now + cooldown,
        )
        remaining = account.failure_cooldown_until - now
        return StoredCooldown(
            scope="health",
            account_id=account.id,
            name="upstream",
            until_epoch=self._wall_time() + remaining,
            failure_count=account.failure_count,
            retain_after_expiry=True,
        )

    @staticmethod
    def _clear_failure_locked(account: _AccountState) -> bool:
        changed = account.failure_count > 0 or account.failure_cooldown_until > 0
        account.failure_count = 0
        account.failure_cooldown_until = 0.0
        return changed

    async def _release(
        self, account_id: str, credential_kind: CredentialKind, key_id: str
    ) -> None:
        async with self._lock:
            key = self._find_key(self._accounts[account_id], credential_kind, key_id)
            key.inflight = max(0, key.inflight - 1)

    @staticmethod
    def _credentials(
        account: _AccountState, credential_kind: CredentialKind
    ) -> list[_KeyState]:
        if credential_kind == "api_key":
            return account.api_keys
        return account.oauth_tokens

    @classmethod
    def _find_key(
        cls,
        account: _AccountState,
        credential_kind: CredentialKind,
        key_id: str,
    ) -> _KeyState:
        return next(
            key
            for key in cls._credentials(account, credential_kind)
            if key.id == key_id
        )

    def _parse_retry_after(
        self, headers: dict[str, str], *, default: float = 1.0
    ) -> float:
        raw_retry = headers.get("retry-after")
        if raw_retry:
            try:
                seconds = float(raw_retry)
            except ValueError:
                try:
                    parsed = parsedate_to_datetime(raw_retry)
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=timezone.utc)
                    seconds = parsed.timestamp() - self._wall_time()
                except (TypeError, ValueError, OverflowError):
                    seconds = math.nan
            if math.isfinite(seconds):
                return max(seconds, 1.0)

        raw_reset = headers.get("x-ratelimit-reset")
        if raw_reset:
            try:
                seconds = float(raw_reset) - self._wall_time()
            except ValueError:
                pass
            else:
                if math.isfinite(seconds):
                    return max(seconds, 1.0)
        return max(default, 1.0)
