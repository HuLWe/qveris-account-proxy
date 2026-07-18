from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Self

import httpx

DEFAULT_USER_AGENT = "qveris-account-proxy/0.1.0"
DEFAULT_ACCEPT_LANGUAGE = "en-US,en;q=0.9"
_MAX_PROXY_REFERENCE_BYTES = 4096
_MANAGER_CLOSE_ATTEMPTS = 2
_PUBLIC_TRANSPORT_ID = "public"

logger = logging.getLogger(__name__)


class TransportConfigurationError(RuntimeError):
    """Raised when an HTTP client configuration cannot be loaded safely."""


class TransportManagerClosed(RuntimeError):
    """Raised when a closed transport manager is used."""


def _validate_header_value(value: str) -> None:
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError:
        raise ValueError("HTTP profile contains an invalid header value") from None
    if not encoded or b"\r" in encoded or b"\n" in encoded:
        raise ValueError("HTTP profile contains an invalid header value")


@dataclass(frozen=True, slots=True)
class HTTPProfile:
    """Stable, non-randomized HTTP identity applied to one client pool."""

    user_agent: str = DEFAULT_USER_AGENT
    accept_language: str = DEFAULT_ACCEPT_LANGUAGE

    def __post_init__(self) -> None:
        _validate_header_value(self.user_agent)
        _validate_header_value(self.accept_language)

    def headers(self) -> Mapping[str, str]:
        return {
            "User-Agent": self.user_agent,
            "Accept-Language": self.accept_language,
        }


@dataclass(frozen=True, slots=True)
class AccountTransportSpec:
    """Configuration needed to build one account-owned HTTP connection pool."""

    account_id: str
    profile: HTTPProfile = field(default_factory=HTTPProfile)
    proxy_url_file: Path | str | None = None

    def __post_init__(self) -> None:
        if not self.account_id or "\r" in self.account_id or "\n" in self.account_id:
            raise ValueError("account transport id is invalid")
        if self.proxy_url_file is not None and not isinstance(
            self.proxy_url_file, Path
        ):
            object.__setattr__(self, "proxy_url_file", Path(self.proxy_url_file))


@dataclass(frozen=True, slots=True)
class ReloadResult:
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    replaced: tuple[str, ...] = ()
    retained: tuple[str, ...] = ()


@dataclass(slots=True)
class _ResolvedSpec:
    spec: AccountTransportSpec
    proxy_url: str | None = field(repr=False)
    proxy_digest: bytes | None = field(repr=False)


@dataclass(slots=True)
class _ClientEntry:
    spec: AccountTransportSpec
    client: httpx.AsyncClient
    transport: httpx.AsyncBaseTransport = field(repr=False)
    proxy_digest: bytes | None = field(repr=False)
    proxy_configured: bool = False
    active_leases: int = 0
    retired: bool = False
    close_started: bool = False
    closed_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    close_state_changed: asyncio.Event = field(
        default_factory=asyncio.Event, repr=False
    )
    close_error: BaseException | None = field(default=None, repr=False)
    close_task: asyncio.Task[None] | None = field(default=None, repr=False)


AccountTransportFactory = Callable[
    [AccountTransportSpec], httpx.AsyncBaseTransport | None
]


class AccountClientLease:
    """Keep one account client alive across a complete response lifecycle."""

    __slots__ = ("_entry", "_manager", "_release_task")

    def __init__(self, manager: AccountTransportManager, entry: _ClientEntry) -> None:
        self._manager = manager
        self._entry = entry
        self._release_task: asyncio.Task[None] | None = None

    @property
    def account_id(self) -> str:
        return self._entry.spec.account_id

    @property
    def client(self) -> httpx.AsyncClient:
        return self._entry.client

    @property
    def released(self) -> bool:
        return self._release_task is not None

    async def release(self) -> None:
        if self._release_task is None:
            self._release_task = asyncio.create_task(
                self._manager._release_entry(self._entry)
            )
        await asyncio.shield(self._release_task)

    async def __aenter__(self) -> httpx.AsyncClient:
        if self._release_task is not None:
            raise RuntimeError("account client lease is already released")
        return self.client

    async def __aexit__(self, *exc_info: object) -> None:
        await self.release()


def _read_proxy_reference(path: Path) -> tuple[str, bytes]:
    try:
        with path.open("rb") as handle:
            encoded = handle.read(_MAX_PROXY_REFERENCE_BYTES + 1)
    except (OSError, UnicodeError):
        raise TransportConfigurationError("proxy URL file is unavailable") from None

    if len(encoded) > _MAX_PROXY_REFERENCE_BYTES:
        raise TransportConfigurationError("proxy URL file is invalid")
    try:
        value = encoded.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise TransportConfigurationError("proxy URL file is invalid") from None
    value = value.strip()
    if not value:
        raise TransportConfigurationError("proxy URL file is empty")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise TransportConfigurationError("proxy URL file is invalid")

    try:
        url = httpx.URL(value)
        if url.scheme not in {"http", "https"} or not url.host:
            raise ValueError
        httpx.Proxy(url)
    except (TypeError, ValueError):
        raise TransportConfigurationError("proxy URL file is invalid") from None

    digest = hashlib.sha256(value.encode("utf-8")).digest()
    return value, digest


class AccountTransportManager:
    """Own isolated account clients and one client for public upstream calls.

    A custom transport factory is intended for tests. Each returned transport is
    owned by its account client and is closed when that client is replaced.
    """

    def __init__(
        self,
        *,
        base_url: str | httpx.URL | None = None,
        timeout: httpx.Timeout | float | None = None,
        limits: httpx.Limits | None = None,
        public_profile: HTTPProfile | None = None,
        transport_factory: AccountTransportFactory | None = None,
        public_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url
        self._timeout = timeout
        self._limits = limits
        self._transport_factory = transport_factory
        self._entries: dict[str, _ClientEntry] = {}
        self._retired: dict[int, _ClientEntry] = {}
        self._reload_lock = asyncio.Lock()
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None
        public_profile = public_profile or HTTPProfile()
        public_client, resolved_public_transport = self._make_client(
            profile=public_profile,
            proxy_url=None,
            transport=public_transport,
        )
        self._public_entry = _ClientEntry(
            spec=AccountTransportSpec(_PUBLIC_TRANSPORT_ID, public_profile),
            client=public_client,
            transport=resolved_public_transport,
            proxy_digest=None,
        )

    @classmethod
    async def create(
        cls,
        specs: Iterable[AccountTransportSpec],
        **kwargs: Any,
    ) -> Self:
        manager = cls(**kwargs)
        try:
            await manager.reload(specs)
        except BaseException:
            await manager.aclose()
            raise
        return manager

    @property
    def public_client(self) -> httpx.AsyncClient:
        if self._closed:
            raise TransportManagerClosed("transport manager is closed")
        return self._public_entry.client

    @property
    def closed(self) -> bool:
        return self._closed

    def account_ids(self) -> tuple[str, ...]:
        return tuple(self._entries)

    def client_for(self, account_id: str) -> httpx.AsyncClient:
        """Return a client without lifecycle protection.

        Request paths that can overlap a reload should use ``acquire`` instead.
        """
        if self._closed:
            raise TransportManagerClosed("transport manager is closed")
        try:
            return self._entries[account_id].client
        except KeyError:
            raise KeyError("unknown account transport") from None

    async def acquire(self, account_id: str) -> AccountClientLease:
        async with self._reload_lock:
            if self._closed:
                raise TransportManagerClosed("transport manager is closed")
            try:
                entry = self._entries[account_id]
            except KeyError:
                raise KeyError("unknown account transport") from None
            entry.active_leases += 1
            return AccountClientLease(self, entry)

    async def acquire_public(self) -> AccountClientLease:
        """Lease the public client across a complete response lifecycle."""
        async with self._reload_lock:
            if self._closed:
                raise TransportManagerClosed("transport manager is closed")
            self._public_entry.active_leases += 1
            return AccountClientLease(self, self._public_entry)

    def profile_for(self, account_id: str) -> HTTPProfile:
        if self._closed:
            raise TransportManagerClosed("transport manager is closed")
        try:
            return self._entries[account_id].spec.profile
        except KeyError:
            raise KeyError("unknown account transport") from None

    def proxy_configured_for(self, account_id: str) -> bool:
        if self._closed:
            raise TransportManagerClosed("transport manager is closed")
        try:
            return self._entries[account_id].proxy_configured
        except KeyError:
            raise KeyError("unknown account transport") from None

    async def reload(self, specs: Iterable[AccountTransportSpec]) -> ReloadResult:
        if self._closed:
            raise TransportManagerClosed("transport manager is closed")
        resolved = self._resolve_specs(specs)
        scheduled_for_close: list[asyncio.Task[None]] = []
        async with self._reload_lock:
            if self._closed:
                raise TransportManagerClosed("transport manager is closed")

            old_entries = self._entries
            new_entries: dict[str, _ClientEntry] = {}
            created: list[_ClientEntry] = []
            added: list[str] = []
            replaced: list[str] = []
            retained: list[str] = []

            try:
                for item in resolved:
                    account_id = item.spec.account_id
                    current = old_entries.get(account_id)
                    if (
                        current is not None
                        and current.spec == item.spec
                        and current.proxy_digest == item.proxy_digest
                    ):
                        new_entries[account_id] = current
                        retained.append(account_id)
                        continue

                    entry = await self._build_entry(item)
                    created.append(entry)
                    new_entries[account_id] = entry
                    if current is None:
                        added.append(account_id)
                    else:
                        replaced.append(account_id)
            except BaseException:
                await self._close_unpublished_entries(created)
                raise

            removed = [
                account_id
                for account_id in old_entries
                if account_id not in new_entries
            ]
            retired = [
                entry
                for account_id, entry in old_entries.items()
                if new_entries.get(account_id) is not entry
            ]
            self._entries = new_entries
            for entry in retired:
                scheduled = self._retire_entry_locked(entry)
                if scheduled is not None:
                    scheduled_for_close.append(scheduled)

            result = ReloadResult(
                added=tuple(added),
                removed=tuple(removed),
                replaced=tuple(replaced),
                retained=tuple(retained),
            )
        await self._wait_for_post_commit_cleanup(scheduled_for_close)
        return result

    async def aclose(self) -> None:
        async with self._reload_lock:
            if self._close_task is None or self._close_task_failed():
                self._closed = True
                for entry in self._entries.values():
                    self._retire_entry_locked(entry)
                self._entries = {}
                self._retire_entry_locked(self._public_entry)
                retired = tuple(self._retired.values())
                self._close_task = asyncio.create_task(self._complete_close(retired))
            close_task = self._close_task
        await asyncio.shield(close_task)

    async def __aenter__(self) -> Self:
        if self._closed:
            raise TransportManagerClosed("transport manager is closed")
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    def _resolve_specs(
        self, specs: Iterable[AccountTransportSpec]
    ) -> list[_ResolvedSpec]:
        resolved: list[_ResolvedSpec] = []
        seen: set[str] = set()
        for spec in specs:
            if not isinstance(spec, AccountTransportSpec):
                raise TypeError("account transport spec is invalid")
            if spec.account_id in seen:
                raise TransportConfigurationError(
                    "account transport ids must be unique"
                )
            seen.add(spec.account_id)
            if spec.proxy_url_file is None:
                proxy_url = None
                proxy_digest = None
            else:
                assert isinstance(spec.proxy_url_file, Path)
                proxy_url, proxy_digest = _read_proxy_reference(spec.proxy_url_file)
            resolved.append(
                _ResolvedSpec(
                    spec=spec,
                    proxy_url=proxy_url,
                    proxy_digest=proxy_digest,
                )
            )
        return resolved

    async def _build_entry(self, item: _ResolvedSpec) -> _ClientEntry:
        transport: httpx.AsyncBaseTransport | None = None
        if self._transport_factory is not None:
            try:
                transport = self._transport_factory(item.spec)
            except Exception:
                raise TransportConfigurationError(
                    "account transport creation failed"
                ) from None
            if transport is not None and not isinstance(
                transport, httpx.AsyncBaseTransport
            ):
                raise TransportConfigurationError("account transport creation failed")
        if transport is not None and item.proxy_url is not None:
            await transport.aclose()
            raise TransportConfigurationError(
                "proxy URL and injected transport cannot be combined"
            )
        try:
            client, resolved_transport = self._make_client(
                profile=item.spec.profile,
                proxy_url=item.proxy_url,
                transport=transport,
            )
        except Exception:
            if transport is not None:
                await transport.aclose()
            raise TransportConfigurationError(
                "account HTTP client configuration is invalid"
            ) from None
        return _ClientEntry(
            spec=item.spec,
            client=client,
            transport=resolved_transport,
            proxy_digest=item.proxy_digest,
            proxy_configured=item.proxy_url is not None,
        )

    def _make_client(
        self,
        *,
        profile: HTTPProfile,
        proxy_url: str | None,
        transport: httpx.AsyncBaseTransport | None,
    ) -> tuple[httpx.AsyncClient, httpx.AsyncBaseTransport]:
        if transport is None:
            transport_kwargs: dict[str, Any] = {"trust_env": False}
            if self._limits is not None:
                transport_kwargs["limits"] = self._limits
            if proxy_url is not None:
                transport_kwargs["proxy"] = proxy_url
            transport = httpx.AsyncHTTPTransport(**transport_kwargs)

        kwargs: dict[str, Any] = {
            "headers": profile.headers(),
            "follow_redirects": False,
            "trust_env": False,
            "transport": transport,
        }
        if self._base_url is not None:
            kwargs["base_url"] = self._base_url
        if self._timeout is not None:
            kwargs["timeout"] = self._timeout
        return httpx.AsyncClient(**kwargs), transport

    async def _release_entry(self, entry: _ClientEntry) -> None:
        scheduled_for_close: asyncio.Task[None] | None = None
        async with self._reload_lock:
            if entry.active_leases <= 0:
                return
            entry.active_leases -= 1
            entry.close_state_changed.set()
            if entry.active_leases == 0 and entry.retired:
                scheduled_for_close = self._schedule_entry_close_locked(entry)
        if scheduled_for_close is not None:
            await self._wait_for_post_commit_cleanup((scheduled_for_close,))

    def _retire_entry_locked(self, entry: _ClientEntry) -> asyncio.Task[None] | None:
        if not entry.retired:
            entry.retired = True
            self._retired[id(entry)] = entry
            entry.close_state_changed.set()
        if entry.active_leases == 0:
            return self._schedule_entry_close_locked(entry)
        return None

    def _schedule_entry_close_locked(
        self, entry: _ClientEntry
    ) -> asyncio.Task[None] | None:
        if entry.closed_event.is_set():
            return None
        if entry.close_task is not None and not entry.close_task.done():
            return entry.close_task
        entry.close_started = True
        entry.close_state_changed.clear()
        task = asyncio.create_task(
            self._finish_entry_close(entry),
            name=f"qveris-close-{entry.spec.account_id}",
        )
        task.add_done_callback(self._consume_cleanup_result)
        entry.close_task = task
        return task

    async def _finish_entry_close(self, entry: _ClientEntry) -> None:
        try:
            if entry.client.is_closed:
                await entry.transport.aclose()
            else:
                await entry.client.aclose()
        except BaseException as exc:
            async with self._reload_lock:
                entry.close_error = exc
                entry.close_started = False
                entry.close_state_changed.set()
            logger.warning(
                "retired HTTP client cleanup failed for %s; "
                "cleanup remains pending error=%s",
                entry.spec.account_id,
                type(exc).__name__,
            )
            raise
        else:
            async with self._reload_lock:
                entry.close_error = None
                entry.closed_event.set()
                entry.close_state_changed.set()
                self._retired.pop(id(entry), None)

    async def _complete_close(
        self,
        retired: Iterable[_ClientEntry],
    ) -> None:
        retired = tuple(retired)
        results = await asyncio.gather(
            *(self._close_entry_for_shutdown(entry) for entry in retired),
            return_exceptions=True,
        )
        errors = [result for result in results if isinstance(result, BaseException)]
        if errors:
            raise errors[0]

    async def _close_entry_for_shutdown(self, entry: _ClientEntry) -> None:
        failures = 0
        while not entry.closed_event.is_set():
            async with self._reload_lock:
                if entry.closed_event.is_set():
                    return
                if entry.active_leases > 0:
                    entry.close_state_changed.clear()
                    close_task = None
                else:
                    close_task = self._schedule_entry_close_locked(entry)

            if close_task is None:
                await entry.close_state_changed.wait()
                continue

            try:
                await asyncio.shield(close_task)
            except BaseException:
                failures += 1
                if failures >= _MANAGER_CLOSE_ATTEMPTS:
                    raise

    async def _wait_for_post_commit_cleanup(
        self, tasks: Iterable[asyncio.Task[None]]
    ) -> None:
        tasks = tuple(tasks)
        if not tasks:
            return
        await asyncio.gather(
            *(asyncio.shield(task) for task in tasks),
            return_exceptions=True,
        )

    def _close_task_failed(self) -> bool:
        if self._close_task is None or not self._close_task.done():
            return False
        if self._close_task.cancelled():
            return True
        return self._close_task.exception() is not None

    @staticmethod
    def _consume_cleanup_result(task: asyncio.Task[None]) -> None:
        try:
            task.exception()
        except asyncio.CancelledError:
            pass

    @staticmethod
    async def _close_unpublished_entries(
        entries: Iterable[_ClientEntry],
    ) -> None:
        clients = [entry.client for entry in entries]
        if not clients:
            return
        results = await asyncio.gather(
            *(client.aclose() for client in clients),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                raise result
