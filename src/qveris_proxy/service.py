from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
from fastapi import HTTPException, Request
from pydantic import ValidationError
from starlette.responses import JSONResponse, StreamingResponse
from starlette.types import Receive, Scope, Send

from .access_keys import (
    ProxyAccessKeyLease,
    ProxyAccessKeyManager,
    ProxyAccessKeyRejected,
)
from .admin import (
    AdminConfigError,
    parse_admin_accounts,
    parse_admin_accounts_submission,
    public_config,
    serialize_accounts,
    write_accounts_atomic,
)
from .config import (
    AccountConfig,
    ConfigurationError,
    ProxySettings,
    QVERIS_BASE_URL,
    parse_accounts_payload,
)
from .gate import ConfigurationGate, ConfigurationLease
from .headers import build_downstream_headers, build_upstream_headers
from .pool import KeyLease, KeyPool, PoolUnavailable
from .reload import CredentialFileReloader, ReloadResult, ReloadStatus
from .routes import Operation
from .state import StateStore
from .transports import (
    AccountClientLease,
    AccountTransportManager,
    AccountTransportSpec,
    HTTPProfile,
)

logger = logging.getLogger(__name__)


class _NonClosingTransport(httpx.AsyncBaseTransport):
    def __init__(self, transport: httpx.AsyncBaseTransport) -> None:
        self._transport = transport

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return await self._transport.handle_async_request(request)

    async def aclose(self) -> None:
        return None


@dataclass(slots=True)
class _ResourceCloser:
    response: httpx.Response | None
    lease: KeyLease | None
    transport_lease: AccountClientLease | None
    configuration_lease: ConfigurationLease
    semaphore: asyncio.Semaphore = field(repr=False)
    access_key_lease: ProxyAccessKeyLease | None = field(repr=False)
    _close_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)

    async def __call__(self) -> None:
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close())
        await asyncio.shield(self._close_task)

    async def _close(self) -> None:
        try:
            if self.response is not None:
                await self.response.aclose()
        finally:
            try:
                if self.transport_lease is not None:
                    await self.transport_lease.release()
            finally:
                try:
                    if self.lease is not None:
                        await self.lease.release()
                finally:
                    try:
                        self.semaphore.release()
                    finally:
                        try:
                            if self.access_key_lease is not None:
                                await self.access_key_lease.release()
                        finally:
                            await self.configuration_lease.release()


class _ClosingStreamingResponse(StreamingResponse):
    def __init__(self, *args: Any, closer: _ResourceCloser, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._closer = closer

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self._closer()


class ProxyService:
    def __init__(
        self,
        settings: ProxySettings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.pool = KeyPool(settings)
        self.state = StateStore(settings.state_path)
        self.proxy_access_keys = ProxyAccessKeyManager(self.state)
        self._base_url = httpx.URL(QVERIS_BASE_URL)
        self._semaphore = asyncio.Semaphore(settings.max_connections)
        self._quota_refresh_lock = asyncio.Lock()
        self._admin_config_lock = asyncio.Lock()
        self._gate = ConfigurationGate()
        self._transition_locks: dict[str, asyncio.Lock] = {}
        self._timeout = httpx.Timeout(
            connect=settings.connect_timeout_seconds,
            read=settings.read_timeout_seconds,
            write=settings.write_timeout_seconds,
            pool=settings.pool_timeout_seconds,
        )
        self._limits = httpx.Limits(
            max_connections=settings.max_connections,
            max_keepalive_connections=max(1, settings.max_connections // 2),
        )
        self._transport_override = transport
        self._transport_override_closed = False
        self._transports: AccountTransportManager | None = None
        self._credential_reloader: CredentialFileReloader | None = None
        if settings.accounts_file_path is not None:
            reload_interval = settings.accounts_reload_interval_seconds or 3600.0
            self._credential_reloader = CredentialFileReloader(
                settings.accounts_file_path,
                self._apply_accounts_payload,
                interval_seconds=reload_interval,
            )

    async def start(self) -> None:
        transport_factory = None
        if self._transport_override is not None:

            def injected_transport(
                spec: AccountTransportSpec,
            ) -> httpx.AsyncBaseTransport | None:
                del spec
                assert self._transport_override is not None
                return _NonClosingTransport(self._transport_override)

            transport_factory = injected_transport
        try:
            await self.proxy_access_keys.initialize(
                self.settings.proxy_access_token.get_secret_value(),
                max_concurrency=self.settings.max_connections,
            )
            self._transports = await AccountTransportManager.create(
                self._transport_specs(self.settings.accounts),
                base_url=self._base_url,
                timeout=self._timeout,
                limits=self._limits,
                transport_factory=transport_factory,
                public_transport=(
                    _NonClosingTransport(self._transport_override)
                    if self._transport_override is not None
                    else None
                ),
            )
            await self.pool.restore_cooldowns(await self.state.load_cooldowns())
            if self._credential_reloader is not None:
                result = await self._credential_reloader.reload(force=True)
                if not result.applied:
                    raise ConfigurationError(
                        "accounts configuration changed during startup"
                    )
        except BaseException:
            try:
                if self._transports is not None:
                    await self._transports.aclose()
            finally:
                try:
                    await self._close_transport_override()
                finally:
                    await self.state.close()
            raise

    async def close(self) -> None:
        try:
            if self._transports is not None:
                await self._transports.aclose()
        finally:
            try:
                await self._close_transport_override()
            finally:
                await self.state.close()

    async def _close_transport_override(self) -> None:
        if self._transport_override is None or self._transport_override_closed:
            return
        self._transport_override_closed = True
        await self._transport_override.aclose()

    @staticmethod
    def _transport_specs(
        accounts: tuple[AccountConfig, ...],
    ) -> tuple[AccountTransportSpec, ...]:
        return tuple(
            AccountTransportSpec(
                account_id=account.id,
                profile=HTTPProfile(
                    user_agent=account.transport.user_agent,
                    accept_language=account.transport.accept_language,
                ),
                proxy_url_file=account.transport.proxy_url_file,
            )
            for account in accounts
        )

    @property
    def transports(self) -> AccountTransportManager:
        if self._transports is None:
            raise RuntimeError("proxy service has not started")
        return self._transports

    @property
    def credential_reload_background_enabled(self) -> bool:
        return (
            self._credential_reloader is not None
            and self.settings.accounts_reload_interval_seconds > 0
        )

    async def run_credential_reloader(self, stop_event: asyncio.Event) -> None:
        if self._credential_reloader is None:
            return
        interval = self.settings.accounts_reload_interval_seconds
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except TimeoutError:
                await self.reload_accounts(force=False)

    async def reload_accounts(self, *, force: bool = True) -> ReloadResult:
        async with self._admin_config_lock:
            return await self._reload_accounts_locked(force=force)

    async def _reload_accounts_locked(self, *, force: bool = True) -> ReloadResult:
        if self._credential_reloader is None:
            return ReloadResult(
                applied=False,
                changed=False,
                generation=0,
                error="reload_not_configured",
            )
        return await self._credential_reloader.reload(force=force)

    async def credential_reload_status(self) -> ReloadStatus:
        if self._credential_reloader is None:
            return ReloadStatus(
                generation=0,
                last_attempt_at=None,
                last_success_at=None,
                error="reload_not_configured",
            )
        return await self._credential_reloader.status()

    async def _apply_accounts_payload(self, payload: bytes) -> None:
        raw_accounts = parse_accounts_payload(payload)
        candidate = self._candidate_settings(raw_accounts)

        candidate_pool: KeyPool | None = None
        if candidate.accounts != self.settings.accounts:
            candidate_pool = KeyPool(candidate)
            await candidate_pool.restore_cooldowns(await self.state.load_cooldowns())

        async with self._gate.update():
            if candidate_pool is not None:
                await candidate_pool.migrate_runtime_from(self.pool)

            reload_task = asyncio.create_task(
                self.transports.reload(self._transport_specs(candidate.accounts))
            )
            cancelled: asyncio.CancelledError | None = None
            try:
                await asyncio.shield(reload_task)
            except asyncio.CancelledError as exc:
                cancelled = exc
                await reload_task

            if candidate_pool is not None:
                self.settings = candidate
                self.pool = candidate_pool
            if cancelled is not None:
                raise cancelled

    def _candidate_settings(self, raw_accounts: Any) -> ProxySettings:
        values = {
            name: getattr(self.settings, name) for name in ProxySettings.model_fields
        }
        values["accounts"] = raw_accounts
        try:
            return ProxySettings.model_validate(values)
        except ValidationError:
            raise ConfigurationError("accounts configuration is invalid") from None

    def admin_config(self) -> dict[str, object]:
        return public_config(self.settings)

    async def admin_config_snapshot(self) -> dict[str, object]:
        async with self._admin_config_lock:
            if self._credential_reloader is not None:
                await self._reload_accounts_locked(force=False)
            config = self.admin_config()
            config["revision"] = self._admin_config_revision()
            return config

    def _admin_config_revision(self) -> str:
        payload = serialize_accounts(self.settings.accounts)
        return hashlib.sha256(payload).hexdigest()

    def validate_admin_config(self, payload: bytes) -> dict[str, object]:
        accounts = parse_admin_accounts(payload, self.settings.accounts)
        try:
            candidate = self._candidate_settings(accounts)
        except ConfigurationError:
            raise AdminConfigError("invalid_config") from None
        return {
            "valid": True,
            "changed": candidate.accounts != self.settings.accounts,
            "account_count": len(candidate.accounts),
            "api_key_count": sum(len(account.keys) for account in candidate.accounts),
            "oauth_token_count": sum(
                len(account.oauth_tokens) for account in candidate.accounts
            ),
        }

    async def save_admin_config(self, payload: bytes) -> ReloadResult:
        if not self.settings.config_write_enabled:
            raise AdminConfigError("persistent_editing_disabled")
        if self.settings.accounts_file_path is None:
            raise AdminConfigError("accounts_file_unavailable")

        async with self._admin_config_lock:
            save_task = asyncio.create_task(self._save_admin_config_locked(payload))
            cancelled: asyncio.CancelledError | None = None
            try:
                result = await asyncio.shield(save_task)
            except asyncio.CancelledError as exc:
                cancelled = exc
                result = await save_task
            if cancelled is not None:
                raise cancelled
            return result

    async def _save_admin_config_locked(self, payload: bytes) -> ReloadResult:
        await self._sync_pending_admin_config_locked()
        accounts, _ = parse_admin_accounts_submission(
            payload,
            self.settings.accounts,
            current_revision=self._admin_config_revision(),
        )
        return await self._persist_admin_accounts_locked(accounts)

    async def _sync_pending_admin_config_locked(self) -> None:
        result = await self._reload_accounts_locked(force=False)
        if result.error is not None:
            raise AdminConfigError("config_reload_failed")

    async def delete_admin_account(self, account_id: str) -> ReloadResult:
        if not self.settings.config_write_enabled:
            raise AdminConfigError("persistent_editing_disabled")
        if self.settings.accounts_file_path is None:
            raise AdminConfigError("accounts_file_unavailable")

        async with self._admin_config_lock:
            delete_task = asyncio.create_task(
                self._delete_admin_account_locked(account_id)
            )
            cancelled: asyncio.CancelledError | None = None
            try:
                result = await asyncio.shield(delete_task)
            except asyncio.CancelledError as exc:
                cancelled = exc
                result = await delete_task
            if cancelled is not None:
                raise cancelled
            return result

    async def _delete_admin_account_locked(self, account_id: str) -> ReloadResult:
        await self._sync_pending_admin_config_locked()
        if not any(account.id == account_id for account in self.settings.accounts):
            raise AdminConfigError("account_not_found")
        if self.settings.default_account == account_id:
            raise AdminConfigError("default_account_locked")
        accounts = tuple(
            account for account in self.settings.accounts if account.id != account_id
        )
        return await self._persist_admin_accounts_locked(accounts)

    async def _persist_admin_accounts_locked(
        self, accounts: tuple[AccountConfig, ...]
    ) -> ReloadResult:
        try:
            candidate = self._candidate_settings(accounts)
        except ConfigurationError:
            raise AdminConfigError("invalid_config") from None
        removed_account_ids = {account.id for account in self.settings.accounts} - {
            account.id for account in candidate.accounts
        }
        current_payload = serialize_accounts(self.settings.accounts)
        candidate_payload = serialize_accounts(candidate.accounts)
        path = self.settings.accounts_file_path
        assert path is not None

        await asyncio.to_thread(write_accounts_atomic, path, candidate_payload)
        result = await self._reload_accounts_locked(force=True)
        if result.error is None:
            applied_pool = self.pool
            try:
                await self._purge_removed_accounts(
                    removed_account_ids, expected_pool=applied_pool
                )
            except Exception as exc:
                logger.error(
                    "removed account state cleanup failed: %s",
                    type(exc).__name__,
                )
            else:
                return result

        try:
            await asyncio.to_thread(write_accounts_atomic, path, current_payload)
            rollback = await self._reload_accounts_locked(force=True)
        except Exception as exc:
            logger.error("admin configuration rollback failed: %s", type(exc).__name__)
            raise AdminConfigError("apply_and_rollback_failed") from None
        if rollback.error is not None:
            raise AdminConfigError("apply_and_rollback_failed")
        raise AdminConfigError("apply_failed")

    async def _purge_removed_accounts(
        self, account_ids: set[str], *, expected_pool: KeyPool
    ) -> None:
        if not account_ids:
            return
        configuration_lease = await self._gate.acquire()
        try:
            if self.pool is not expected_pool:
                raise RuntimeError("account configuration changed during cleanup")
            await self.state.purge_accounts(account_ids)
        finally:
            await configuration_lease.release()

    def authenticate(self, request: Request) -> None:
        raw = request.headers.get("authorization", "")
        scheme, separator, candidate = raw.partition(" ")
        expected = self.settings.proxy_access_token.get_secret_value()
        valid = (
            separator == " "
            and scheme.lower() == "bearer"
            and hmac.compare_digest(candidate, expected)
        )
        if not valid:
            raise HTTPException(
                status_code=401,
                detail="proxy authentication required",
                headers={"WWW-Authenticate": "Bearer"},
            )

    async def authenticate_proxy(self, request: Request) -> ProxyAccessKeyLease:
        raw = request.headers.get("authorization", "")
        scheme, separator, candidate = raw.partition(" ")
        if separator != " " or scheme.lower() != "bearer" or not candidate:
            self._raise_proxy_authentication_required()
        try:
            return await self.proxy_access_keys.acquire(candidate)
        except ProxyAccessKeyRejected as exc:
            self._raise_proxy_access_key_rejected(exc)
        raise AssertionError("unreachable")

    async def test_proxy_key(self, request: Request) -> dict[str, object]:
        raw = request.headers.get("authorization", "")
        scheme, separator, candidate = raw.partition(" ")
        if separator != " " or scheme.lower() != "bearer" or not candidate:
            self._raise_proxy_authentication_required()
        try:
            key = await self.proxy_access_keys.inspect(candidate)
        except ProxyAccessKeyRejected as exc:
            self._raise_proxy_access_key_rejected(exc)
        return {
            "status": "ok",
            "key": {
                "id": key.id,
                "kind": key.kind,
                "name": key.name,
            },
        }

    @classmethod
    def _raise_proxy_access_key_rejected(cls, exc: ProxyAccessKeyRejected) -> None:
        if exc.reason in {"invalid", "disabled", "expired"}:
            cls._raise_proxy_authentication_required()
        detail = {
            "request_limit": "proxy API key usage limit reached",
            "rate_limit": "proxy API key rate limit reached",
            "concurrency": "proxy API key concurrency limit reached",
        }[exc.reason]
        headers = (
            {"Retry-After": str(exc.retry_after)}
            if exc.retry_after is not None
            else None
        )
        raise HTTPException(
            status_code=429,
            detail=detail,
            headers=headers,
        )

    @staticmethod
    def _raise_proxy_authentication_required() -> None:
        raise HTTPException(
            status_code=401,
            detail="proxy authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    def resolve_explicit_account(self, request: Request) -> str | None:
        account_id = request.headers.get("x-qveris-account")
        if account_id is not None and not self.pool.has_account(account_id):
            raise HTTPException(status_code=400, detail="unknown QVeris account route")
        return account_id

    async def forward(
        self, request: Request, operation: Operation
    ) -> StreamingResponse:
        access_key_lease: ProxyAccessKeyLease | None = None
        if operation.proxy_auth:
            access_key_lease = await self.authenticate_proxy(request)
        try:
            body = await self._read_request_body(request)
        except BaseException:
            if access_key_lease is not None:
                await access_key_lease.release()
            raise
        affinity_values = self._request_affinity_values(request, body)

        try:
            await asyncio.wait_for(
                self._semaphore.acquire(),
                timeout=self.settings.queue_timeout_seconds,
            )
        except TimeoutError:
            if access_key_lease is not None:
                await access_key_lease.release()
            raise HTTPException(status_code=503, detail="proxy is busy") from None

        try:
            configuration_lease = await self._gate.acquire()
        except BaseException:
            self._semaphore.release()
            if access_key_lease is not None:
                await access_key_lease.release()
            raise

        request_pool = self.pool
        lease: KeyLease | None = None
        transport_lease: AccountClientLease | None = None
        account_id: str | None = None
        explicit_account: str | None = None
        affinity_account: str | None = None
        response: httpx.Response | None = None
        try:
            try:
                if operation.provider_auth:
                    if not request_pool.account_ids():
                        raise HTTPException(
                            status_code=503,
                            detail="no QVeris accounts are configured",
                            headers={"Retry-After": "1"},
                        )
                    credential_kind = operation.credential_kind
                    assert credential_kind is not None
                    fallback_credential_kind = (
                        "api_key"
                        if (
                            credential_kind == "oauth"
                            and self.settings.allow_oauth_route_fallback
                            and operation.route_id == "auth/usage/history/v2"
                        )
                        else None
                    )
                    explicit_account = self.resolve_explicit_account(request)
                    if explicit_account is not None:
                        account_id = explicit_account
                        lease = await self.pool.acquire(
                            account_id,
                            operation.route_id,
                            credential_kind,
                            credit_sensitive=operation.credit_sensitive,
                            fallback_credential_kind=fallback_credential_kind,
                        )
                    else:
                        affinity_account = await self._lookup_affinity_account(
                            affinity_values
                        )
                        if affinity_account is not None:
                            account_id = affinity_account
                            lease = await self.pool.acquire(
                                account_id,
                                operation.route_id,
                                credential_kind,
                                credit_sensitive=operation.credit_sensitive,
                                fallback_credential_kind=fallback_credential_kind,
                            )
                        elif (
                            operation.auto_route
                            and self.settings.routing_mode == "round_robin"
                        ):
                            lease = await self.pool.acquire_any(
                                operation.route_id,
                                credential_kind,
                                credit_sensitive=operation.credit_sensitive,
                                fallback_credential_kind=fallback_credential_kind,
                            )
                            account_id = lease.account_id
                        elif self.settings.effective_default_account is not None:
                            account_id = self.settings.effective_default_account
                            lease = await self.pool.acquire(
                                account_id,
                                operation.route_id,
                                credential_kind,
                                credit_sensitive=operation.credit_sensitive,
                                fallback_credential_kind=fallback_credential_kind,
                            )
                        else:
                            raise HTTPException(
                                status_code=400,
                                detail=(
                                    "X-QVeris-Account is required for this request"
                                ),
                            )
            except PoolUnavailable as exc:
                status_code = {
                    "credits": 402,
                    "rate_limit": 429,
                }.get(exc.reason, 503)
                detail = {
                    "credits": "selected QVeris account has insufficient credits",
                    "missing_credentials": (
                        "selected QVeris account has no credential for this route"
                    ),
                }.get(exc.reason, "selected QVeris account is cooling down")
                headers = {"Retry-After": str(exc.retry_after)}
                if account_id is not None:
                    headers["X-QVeris-Proxy-Account"] = account_id
                raise HTTPException(
                    status_code=status_code,
                    detail=detail,
                    headers=headers,
                ) from None

            affinity_ttl_seconds = self.settings.affinity_ttl_seconds
            affinity_capture_bytes = self.settings.affinity_capture_bytes

            url = self._base_url.join(operation.upstream_path)
            raw_query = request.scope.get("query_string", b"")
            if raw_query:
                url = url.copy_with(query=raw_query)
            if account_id is None:
                transport_lease = await self.transports.acquire_public()
            else:
                transport_lease = await self.transports.acquire(account_id)
            upstream_client = transport_lease.client
            upstream_request = upstream_client.build_request(
                request.method,
                url,
                headers=build_upstream_headers(
                    request.headers,
                    lease.bearer_token if lease is not None else None,
                ),
                content=body if body else None,
            )
            response = await upstream_client.send(upstream_request, stream=True)
            response_headers = build_downstream_headers(response.headers)
            if lease is not None:
                await self._report_response(
                    lease,
                    response.status_code,
                    {name.lower(): value for name, value in response.headers.items()},
                    expected_pool=request_pool,
                )

            failover_allowed = (
                operation.same_request_failover
                and explicit_account is None
                and affinity_account is None
                and self.settings.routing_mode == "round_robin"
                and response.status_code in {401, 402, 403, 429, 500, 502, 503, 504}
            )
            if failover_allowed and lease is not None:
                attempted_account = lease.account_id
                try:
                    retry_lease = await request_pool.acquire_any(
                        operation.route_id,
                        operation.credential_kind,
                        credit_sensitive=operation.credit_sensitive,
                    )
                except PoolUnavailable:
                    retry_lease = None
                if retry_lease is not None:
                    if retry_lease.account_id == attempted_account:
                        await retry_lease.release()
                    else:
                        await response.aclose()
                        await transport_lease.release()
                        await lease.release()
                        lease = retry_lease
                        account_id = retry_lease.account_id
                        transport_lease = await self.transports.acquire(account_id)
                        upstream_client = transport_lease.client
                        upstream_request = upstream_client.build_request(
                            request.method,
                            url,
                            headers=build_upstream_headers(
                                request.headers, retry_lease.bearer_token
                            ),
                            content=body if body else None,
                        )
                        response = await upstream_client.send(
                            upstream_request, stream=True
                        )
                        await self._report_response(
                            retry_lease,
                            response.status_code,
                            {
                                name.lower(): value
                                for name, value in response.headers.items()
                            },
                            expected_pool=request_pool,
                        )
                        response_headers = build_downstream_headers(response.headers)

            if account_id is not None:
                await self.state.set_affinities(
                    affinity_values,
                    account_id,
                    self.settings.affinity_ttl_seconds,
                )
            await configuration_lease.release()

            closer = _ResourceCloser(
                response,
                lease,
                transport_lease,
                configuration_lease,
                self._semaphore,
                access_key_lease,
            )
            if account_id is not None:
                response_headers["x-qveris-proxy-account"] = account_id
            captured = bytearray()
            capture_enabled = (
                account_id is not None
                and "json" in response.headers.get("content-type", "").lower()
            )

            async def raw_body():
                completed = False
                try:
                    if request.method != "HEAD" and response.status_code not in (
                        204,
                        304,
                    ):
                        if response.is_stream_consumed:
                            if response.content:
                                if capture_enabled:
                                    captured.extend(
                                        response.content[:affinity_capture_bytes]
                                    )
                                yield response.content
                        else:
                            async for chunk in response.aiter_raw():
                                if (
                                    capture_enabled
                                    and len(captured) < affinity_capture_bytes
                                ):
                                    remaining = affinity_capture_bytes - len(captured)
                                    captured.extend(chunk[:remaining])
                                yield chunk
                    completed = True
                except (httpx.TimeoutException, httpx.TransportError):
                    if response.status_code < 500:
                        await self._save_transport_failure(
                            lease, expected_pool=request_pool
                        )
                    raise
                finally:
                    try:
                        if completed and account_id is not None and captured:
                            await self._set_affinities_for_pool(
                                self._response_affinity_values(bytes(captured)),
                                account_id,
                                affinity_ttl_seconds,
                                expected_pool=request_pool,
                            )
                    finally:
                        await closer()

            return _ClosingStreamingResponse(
                raw_body(),
                status_code=response.status_code,
                headers=response_headers,
                closer=closer,
            )
        except HTTPException:
            await self._cleanup_failed_request(
                response,
                lease,
                transport_lease,
                configuration_lease,
                access_key_lease,
            )
            raise
        except httpx.TimeoutException:
            await self._save_transport_failure(lease, expected_pool=request_pool)
            await self._cleanup_failed_request(
                response,
                lease,
                transport_lease,
                configuration_lease,
                access_key_lease,
            )
            raise HTTPException(
                status_code=504, detail="QVeris upstream timed out"
            ) from None
        except httpx.TransportError:
            await self._save_transport_failure(lease, expected_pool=request_pool)
            await self._cleanup_failed_request(
                response,
                lease,
                transport_lease,
                configuration_lease,
                access_key_lease,
            )
            raise HTTPException(
                status_code=502, detail="QVeris upstream is unavailable"
            ) from None
        except BaseException:
            await self._cleanup_failed_request(
                response,
                lease,
                transport_lease,
                configuration_lease,
                access_key_lease,
            )
            raise

    async def _cleanup_failed_request(
        self,
        response: httpx.Response | None,
        lease: KeyLease | None,
        transport_lease: AccountClientLease | None,
        configuration_lease: ConfigurationLease,
        access_key_lease: ProxyAccessKeyLease | None,
    ) -> None:
        await _ResourceCloser(
            response,
            lease,
            transport_lease,
            configuration_lease,
            self._semaphore,
            access_key_lease,
        )()

    async def _set_affinities_for_pool(
        self,
        values: set[str],
        account_id: str,
        ttl_seconds: float,
        *,
        expected_pool: KeyPool,
    ) -> None:
        configuration_lease = await self._gate.acquire()
        try:
            if self.pool is not expected_pool or not expected_pool.has_account(
                account_id
            ):
                return
            await self.state.set_affinities(values, account_id, ttl_seconds)
        finally:
            await configuration_lease.release()

    async def _save_transport_failure(
        self, lease: KeyLease | None, *, expected_pool: KeyPool
    ) -> None:
        if lease is None:
            return
        try:
            configuration_lease = await self._gate.acquire()
            try:
                if self.pool is not expected_pool or not expected_pool.has_account(
                    lease.account_id
                ):
                    return
                async with self._transition_lock(lease.account_id):
                    transition = await lease.report_transport_failure()
                    await self.state.save_cooldown(transition)
                    await expected_pool.restore_cooldowns([transition])
            finally:
                await configuration_lease.release()
        except Exception as exc:
            logger.warning(
                "failed to persist upstream transport state: %s",
                type(exc).__name__,
            )

    async def _report_response(
        self,
        lease: KeyLease,
        status_code: int,
        headers: dict[str, str],
        *,
        expected_pool: KeyPool,
    ) -> None:
        if self.pool is not expected_pool or not expected_pool.has_account(
            lease.account_id
        ):
            return
        async with self._transition_lock(lease.account_id):
            transition = await lease.report_response(status_code, headers)
            if transition is not None:
                await self.state.save_cooldown(transition)

    async def _save_quota_observation(
        self,
        lease: KeyLease,
        http_status: int,
        snapshot: dict[str, object],
        balance: float | None,
        *,
        expected_pool: KeyPool,
    ) -> None:
        if self.pool is not expected_pool or not expected_pool.has_account(
            lease.account_id
        ):
            return
        async with self._transition_lock(lease.account_id):
            transition = (
                await lease.report_credit_balance(balance)
                if http_status == 200 and balance is not None
                else None
            )
            await self.state.save_quota_observation(
                lease.account_id,
                http_status,
                snapshot,
                valid_snapshot=http_status == 200 and balance is not None,
                transition=transition,
            )

    async def _save_quota_snapshot_for_pool(
        self,
        account_id: str,
        http_status: int,
        snapshot: dict[str, object],
        *,
        expected_pool: KeyPool,
    ) -> None:
        if self.pool is not expected_pool or not expected_pool.has_account(account_id):
            return
        await self.state.save_quota_snapshot(account_id, http_status, snapshot)

    def _transition_lock(self, account_id: str) -> asyncio.Lock:
        return self._transition_locks.setdefault(account_id, asyncio.Lock())

    async def _read_request_body(self, request: Request) -> bytes:
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                declared_length = int(content_length)
            except ValueError:
                raise HTTPException(
                    status_code=400, detail="invalid Content-Length"
                ) from None
            if declared_length < 0:
                raise HTTPException(status_code=400, detail="invalid Content-Length")
            if declared_length > self.settings.max_request_body_bytes:
                raise HTTPException(status_code=413, detail="request body is too large")

        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > self.settings.max_request_body_bytes:
                raise HTTPException(status_code=413, detail="request body is too large")
        return bytes(body)

    async def _lookup_affinity_account(self, values: set[str]) -> str | None:
        for value in sorted(values):
            account_id = await self.state.get_affinity(value)
            if account_id is not None and self.pool.has_account(account_id):
                return account_id
        return None

    @staticmethod
    def _request_affinity_values(request: Request, body: bytes) -> set[str]:
        values: set[str] = set()
        header_session = request.headers.get("x-qveris-session")
        if header_session:
            values.add(f"client:{header_session}")

        for name in ("session_id", "search_id", "execution_id"):
            query_value = request.query_params.get(name)
            if query_value:
                values.add(f"{name}:{query_value}")

        if body and "json" in request.headers.get("content-type", "").lower():
            try:
                payload = json.loads(body)
            except (json.JSONDecodeError, UnicodeDecodeError):
                payload = None
            if isinstance(payload, dict):
                for name in ("session_id", "search_id", "execution_id"):
                    value = payload.get(name)
                    if isinstance(value, str) and value:
                        values.add(f"{name}:{value}")
        return values

    @classmethod
    def _response_affinity_values(cls, body: bytes) -> set[str]:
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return set()

        values: set[str] = set()

        def visit(value: Any, depth: int = 0) -> None:
            if depth > 6:
                return
            if isinstance(value, dict):
                for name, child in value.items():
                    if (
                        name in {"session_id", "search_id", "execution_id"}
                        and isinstance(child, str)
                        and child
                    ):
                        values.add(f"{name}:{child}")
                    elif isinstance(child, (dict, list)):
                        visit(child, depth + 1)
            elif isinstance(value, list):
                for child in value[:100]:
                    visit(child, depth + 1)

        visit(payload)
        return values

    async def refresh_quotas(self) -> list[dict[str, object]]:
        async with self._quota_refresh_lock:
            return await self._refresh_quotas()

    async def quota_pool_status(
        self, *, refresh_expired: bool = False
    ) -> dict[str, object]:
        account_ids = self.pool.account_ids()
        configured_accounts = len(account_ids)
        snapshots = await self.state.quota_snapshots()
        if refresh_expired and account_ids:
            now = self.state.now()
            refresh_interval = self.settings.quota_refresh_interval_seconds
            accounts_to_refresh = tuple(
                account_id
                for account_id in account_ids
                if (
                    account_id not in snapshots
                    or refresh_interval == 0
                    or now - float(snapshots[account_id]["checked_at"])
                    >= refresh_interval
                )
            )
            if accounts_to_refresh:
                await self._refresh_quotas_for_accounts(accounts_to_refresh)
                snapshots = await self.state.quota_snapshots()

        balances: list[float] = []
        stale = False
        snapshot_times: list[float] = []
        for account_id in account_ids:
            result = snapshots.get(account_id)
            if result is None:
                continue
            snapshot = result.get("credits")
            if not isinstance(snapshot, dict):
                continue
            balance = self._snapshot_credit_balance(snapshot)
            last_success_at = result.get("last_success_at")
            if balance is None or not isinstance(last_success_at, (int, float)):
                continue
            balances.append(max(0.0, balance))
            snapshot_times.append(float(last_success_at))
            stale = stale or bool(result.get("stale"))

        total: int | float | None = None
        if balances:
            summed = math.fsum(balances)
            total = int(summed) if summed.is_integer() else summed
        partial = bool(configured_accounts) and (
            len(balances) != configured_accounts or stale
        )
        return {
            "total_available_credits": total,
            "configured_accounts": configured_accounts,
            "included_accounts": len(balances),
            "complete": bool(configured_accounts) and not partial,
            "partial": partial,
            "stale": stale,
            "snapshot_at": max(snapshot_times) if snapshot_times else None,
            "refresh_interval_seconds": self.settings.quota_refresh_interval_seconds,
        }

    async def aggregate_credits(self, request: Request) -> JSONResponse:
        access_key_lease = await self.authenticate_proxy(request)
        try:
            pool = await self.quota_pool_status(refresh_expired=True)
            if pool["configured_accounts"] == 0:
                raise HTTPException(
                    status_code=503,
                    detail="no QVeris accounts are configured",
                    headers={"Retry-After": "1", "Cache-Control": "no-store"},
                )
            total = pool["total_available_credits"]
            if total is None:
                raise HTTPException(
                    status_code=503,
                    detail="QVeris credit balances are unavailable",
                    headers={"Retry-After": "1", "Cache-Control": "no-store"},
                )
            return JSONResponse(
                {
                    "status": "success",
                    "data": {
                        "remaining_credits": total,
                        "total_available_credits": total,
                    },
                    "proxy_pool": {
                        field: pool[field]
                        for field in (
                            "configured_accounts",
                            "included_accounts",
                            "complete",
                            "partial",
                            "stale",
                            "snapshot_at",
                        )
                    },
                },
                headers={"Cache-Control": "no-store"},
            )
        finally:
            await access_key_lease.release()

    async def test_account(self, account_id: str) -> dict[str, object]:
        configuration_lease = await self._gate.acquire()
        try:
            account = next(
                (
                    candidate
                    for candidate in self.settings.accounts
                    if candidate.id == account_id
                ),
                None,
            )
            if account is None:
                raise KeyError("unknown account")
            credential_kinds = tuple(
                kind
                for kind, configured in (
                    ("api_key", bool(account.keys)),
                    ("oauth", bool(account.oauth_tokens)),
                )
                if configured
            )
        finally:
            await configuration_lease.release()

        checks = [
            await self._test_account_credential(account_id, credential_kind)
            for credential_kind in credential_kinds
        ]
        return {
            "account": account_id,
            "ok": bool(checks) and all(bool(check["ok"]) for check in checks),
            "checks": checks,
        }

    async def _test_account_credential(
        self, account_id: str, credential_kind: str
    ) -> dict[str, object]:
        started = time.perf_counter()
        route_id = f"admin/test/{credential_kind}"
        method = "GET" if credential_kind == "api_key" else "POST"
        upstream_path = (
            "auth/credits" if credential_kind == "api_key" else "auth/verify-token"
        )
        configuration_lease = await self._gate.acquire()
        request_pool = self.pool
        lease: KeyLease | None = None
        transport_lease: AccountClientLease | None = None
        response: httpx.Response | None = None
        try:
            if not self.pool.has_account(account_id):
                return self._test_result(
                    credential_kind,
                    started,
                    ok=False,
                    http_status=0,
                    reason="account_changed",
                )
            lease = await self.pool.acquire(
                account_id,
                route_id,
                credential_kind,
                control=True,
            )
            transport_lease = await self.transports.acquire(account_id)
            request = transport_lease.client.build_request(
                method,
                self._base_url.join(upstream_path),
                headers=build_upstream_headers({}, lease.bearer_token),
            )
            response = await transport_lease.client.send(request)
            await self._report_response(
                lease,
                response.status_code,
                {name.lower(): value for name, value in response.headers.items()},
                expected_pool=request_pool,
            )
            result = self._test_result(
                credential_kind,
                started,
                ok=200 <= response.status_code < 300,
                http_status=response.status_code,
                reason=(
                    "ok" if 200 <= response.status_code < 300 else "upstream_rejected"
                ),
                credential_id=lease.key_id,
            )
            if credential_kind == "api_key":
                snapshot = self._credit_snapshot(response)
                balance = self._snapshot_credit_balance(snapshot)
                await self._save_quota_observation(
                    lease,
                    response.status_code,
                    snapshot,
                    balance,
                    expected_pool=request_pool,
                )
                result["credits"] = snapshot
            return result
        except PoolUnavailable as exc:
            return self._test_result(
                credential_kind,
                started,
                ok=False,
                http_status=0,
                reason=exc.reason,
                retry_after=exc.retry_after,
            )
        except (httpx.TimeoutException, httpx.TransportError):
            await self._save_transport_failure(lease, expected_pool=request_pool)
            return self._test_result(
                credential_kind,
                started,
                ok=False,
                http_status=0,
                reason="transport_error",
                credential_id=lease.key_id if lease is not None else None,
            )
        finally:
            try:
                if response is not None:
                    await response.aclose()
            finally:
                try:
                    if transport_lease is not None:
                        await transport_lease.release()
                finally:
                    try:
                        if lease is not None:
                            await lease.release()
                    finally:
                        await configuration_lease.release()

    @staticmethod
    def _test_result(
        credential_kind: str,
        started: float,
        *,
        ok: bool,
        http_status: int,
        reason: str,
        credential_id: str | None = None,
        retry_after: int | None = None,
    ) -> dict[str, object]:
        result: dict[str, object] = {
            "credential_kind": credential_kind,
            "ok": ok,
            "http_status": http_status,
            "reason": reason,
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
        }
        if credential_id is not None:
            result["credential_id"] = credential_id
        if retry_after is not None:
            result["retry_after"] = retry_after
        return result

    async def _refresh_quotas(self) -> list[dict[str, object]]:
        configuration_lease = await self._gate.acquire()
        try:
            account_ids = self.pool.account_ids()
        finally:
            await configuration_lease.release()
        return await self._refresh_quotas_current_generation(account_ids)

    async def _refresh_quotas_for_accounts(
        self, account_ids: tuple[str, ...]
    ) -> list[dict[str, object]]:
        async with self._quota_refresh_lock:
            return await self._refresh_quotas_current_generation(account_ids)

    async def _refresh_quotas_current_generation(
        self, account_ids: tuple[str, ...]
    ) -> list[dict[str, object]]:
        results: list[dict[str, object]] = []
        for account_id in account_ids:
            configuration_lease = await self._gate.acquire()
            request_pool = self.pool
            lease: KeyLease | None = None
            transport_lease: AccountClientLease | None = None
            response: httpx.Response | None = None
            try:
                if not self.pool.has_account(account_id):
                    continue
                lease = await self.pool.acquire(
                    account_id,
                    "auth/credits",
                    "api_key",
                    control=True,
                )
                transport_lease = await self.transports.acquire(account_id)
                client = transport_lease.client
                request = client.build_request(
                    "GET",
                    self._base_url.join("auth/credits"),
                    headers=build_upstream_headers({}, lease.bearer_token),
                )
                response = await client.send(request)
                await self._report_response(
                    lease,
                    response.status_code,
                    {name.lower(): value for name, value in response.headers.items()},
                    expected_pool=request_pool,
                )
                snapshot = self._credit_snapshot(response)
                balance = self._snapshot_credit_balance(snapshot)
                await self._save_quota_observation(
                    lease,
                    response.status_code,
                    snapshot,
                    balance,
                    expected_pool=request_pool,
                )
                results.append(
                    {
                        "account": account_id,
                        "http_status": response.status_code,
                        "credits": snapshot,
                    }
                )
            except PoolUnavailable as exc:
                status_code = 429 if exc.reason == "rate_limit" else 503
                await self._save_quota_snapshot_for_pool(
                    account_id, status_code, {}, expected_pool=request_pool
                )
                results.append(
                    {
                        "account": account_id,
                        "http_status": status_code,
                        "credits": {},
                    }
                )
            except (httpx.TimeoutException, httpx.TransportError):
                await self._save_transport_failure(lease, expected_pool=request_pool)
                await self._save_quota_snapshot_for_pool(
                    account_id, 0, {}, expected_pool=request_pool
                )
                results.append({"account": account_id, "http_status": 0, "credits": {}})
            finally:
                try:
                    if response is not None:
                        await response.aclose()
                finally:
                    try:
                        if transport_lease is not None:
                            await transport_lease.release()
                    finally:
                        try:
                            if lease is not None:
                                await lease.release()
                        finally:
                            await configuration_lease.release()
        return results

    @staticmethod
    def _credit_snapshot(response: httpx.Response) -> dict[str, object]:
        if response.status_code != 200:
            return {}
        try:
            payload = response.json()
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            return {}

        allowed = {
            "available_credits",
            "balance",
            "credits",
            "daily_credits",
            "remaining_credits",
            "total_available_credits",
            "total_credits",
        }
        snapshot: dict[str, object] = {}

        def visit(value: Any, prefix: str = "", depth: int = 0) -> None:
            if depth > 6 or len(snapshot) >= 32:
                return
            if isinstance(value, dict):
                for name, child in value.items():
                    path = f"{prefix}.{name}" if prefix else name
                    if name.lower() in allowed and isinstance(child, (int, float, str)):
                        snapshot[path] = child
                    elif isinstance(child, dict):
                        visit(child, path, depth + 1)

        visit(payload)
        return snapshot

    @staticmethod
    def _snapshot_credit_balance(snapshot: dict[str, object]) -> float | None:
        preferred = (
            "remaining_credits",
            "total_available_credits",
            "available_credits",
            "balance",
            "credits",
        )
        ordered = sorted(
            snapshot.items(),
            key=lambda item: next(
                (
                    index
                    for index, name in enumerate(preferred)
                    if item[0].lower().endswith(name)
                ),
                len(preferred),
            ),
        )
        for _, value in ordered:
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                numeric = float(value)
            elif isinstance(value, str):
                try:
                    numeric = float(value)
                except ValueError:
                    continue
            else:
                continue
            if math.isfinite(numeric):
                return numeric
        return None

    async def account_status(self) -> list[dict[str, object]]:
        configuration_lease = await self._gate.acquire()
        try:
            quotas = await self.state.quota_snapshots()
            pool_status = await self.pool.status()
            if not self.settings.config_write_enabled:
                edit_reason = "persistent_editing_disabled"
            elif self.settings.accounts_file_path is None:
                edit_reason = "accounts_file_unavailable"
            else:
                edit_reason = None
            account_names = {
                account.id: account.name or account.id
                for account in self.settings.accounts
            }
            for account in pool_status:
                account["quota"] = quotas.get(str(account["id"]))
                account_id = str(account["id"])
                account["name"] = account_names.get(account_id, account_id)
                account["network"] = {
                    "proxy_configured": self.transports.proxy_configured_for(
                        account_id
                    ),
                    "accept_language": self.transports.profile_for(
                        account_id
                    ).accept_language,
                }
                delete_reason = edit_reason
                if (
                    delete_reason is None
                    and self.settings.default_account == account_id
                ):
                    delete_reason = "default_account_locked"
                account["management"] = {
                    "can_edit": edit_reason is None,
                    "edit_reason": edit_reason,
                    "can_delete": delete_reason is None,
                    "delete_reason": delete_reason,
                }
            return pool_status
        finally:
            await configuration_lease.release()
