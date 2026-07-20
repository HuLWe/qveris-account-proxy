from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from .browser import (
    AccountBrowser,
    BrowserRuntime,
    PlaywrightBrowserRuntime,
    SessionObservation,
)
from .config import (
    BrowserAccountConfig,
    ConfigurationError,
    KeeperSettings,
    read_secret,
    resolve_timezone,
)
from .state import AccountStatus, KeeperStateStore

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _ManagedAccount:
    config: BrowserAccountConfig
    status: AccountStatus
    browser: AccountBrowser | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class KeeperService:
    def __init__(
        self,
        settings: KeeperSettings,
        *,
        runtime: BrowserRuntime | None = None,
        wall_time: Callable[[], float] = time.time,
    ) -> None:
        self.settings = settings
        self._runtime = runtime or PlaywrightBrowserRuntime()
        self._wall_time = wall_time
        self._store = KeeperStateStore(settings.state_path, wall_time=wall_time)
        self._accounts = {
            account.id: _ManagedAccount(account, AccountStatus(account_id=account.id))
            for account in settings.accounts
        }
        self._scheduler_task: asyncio.Task[None] | None = None
        self._due_lock = asyncio.Lock()
        self._stop_event = asyncio.Event()
        self._started = False
        self._closed = False

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        persisted = await self._store.load(set(self._accounts))
        for account_id, managed in self._accounts.items():
            previous = persisted.get(account_id)
            if previous is not None:
                managed.status = previous.evolve(
                    state="starting",
                    reason="startup",
                    updated_at=self._wall_time(),
                )
            managed.status = await self._store.save(managed.status)

        await asyncio.gather(
            *(self.refresh_account(account_id) for account_id in self._accounts)
        )
        self._scheduler_task = asyncio.create_task(
            self._scheduler_loop(), name="qveris-keeper-scheduler"
        )

    async def _run_action(self, awaitable):
        return await asyncio.wait_for(
            awaitable, timeout=self.settings.action_timeout_seconds
        )

    async def _ensure_browser(self, managed: _ManagedAccount) -> AccountBrowser:
        if managed.browser is None:
            managed.browser = await self._run_action(self._runtime.open(managed.config))
        return managed.browser

    async def _bootstrap_if_configured(
        self, managed: _ManagedAccount, browser: AccountBrowser
    ) -> bool:
        path = managed.config.bootstrap_token_file
        if path is None:
            return False
        token = read_secret(path, "bootstrap token", min_length=8)
        await self._run_action(browser.bootstrap_token(token))
        return True

    async def _email_login(
        self, managed: _ManagedAccount, browser: AccountBrowser
    ) -> SessionObservation:
        config = managed.config
        if config.email_file is None or config.password_file is None:
            raise ConfigurationError("email login secret files are unavailable")
        email = read_secret(config.email_file, "account email", min_length=3)
        password = read_secret(config.password_file, "account password", min_length=1)
        return await self._run_action(browser.login_email(email, password))

    def _backoff(self, failures: int) -> float:
        exponent = min(max(failures - 1, 0), 20)
        return min(
            self.settings.retry_max_seconds,
            self.settings.retry_base_seconds * (2**exponent),
        )

    async def _save_observation(
        self,
        managed: _ManagedAccount,
        observation: SessionObservation,
        *,
        login_succeeded: bool = False,
        touched: bool = False,
        reason: str | None = None,
    ) -> dict[str, object]:
        now = self._wall_time()
        previous = managed.status
        common = {
            "verify_http_status": observation.verify_http_status,
            "userinfo_http_status": observation.userinfo_http_status,
            "last_probe_at": now,
            "updated_at": now,
        }
        if observation.kind == "authenticated":
            changes: dict[str, object] = {
                **common,
                "state": "authenticated",
                "reason": reason or "ok",
                "last_authenticated_at": now,
                "failure_count": 0,
                "next_action_at": 0.0,
            }
            if login_succeeded:
                changes["last_login_at"] = now
            if touched:
                local = datetime.fromtimestamp(
                    now, resolve_timezone(managed.config.timezone_id)
                )
                changes["last_touch_at"] = now
                changes["last_touch_local_date"] = local.date().isoformat()
            status = previous.evolve(**changes)
        else:
            failures = previous.failure_count + 1
            if observation.kind == "challenge":
                state = "challenge_required"
                reason_code = "challenge_detected"
                retry = self.settings.retry_max_seconds
            elif observation.kind == "unauthenticated":
                state = "manual_action_required"
                reason_code = reason or "session_missing"
                retry = self._backoff(failures)
            else:
                state = "degraded"
                reason_code = reason or "rpc_unavailable"
                retry = self._backoff(failures)
            status = previous.evolve(
                **common,
                state=state,
                reason=reason_code,
                failure_count=failures,
                next_action_at=now + retry,
            )
        managed.status = await self._store.save(status)
        return self._render_status(managed.status)

    async def _save_failure(
        self, managed: _ManagedAccount, reason: str
    ) -> dict[str, object]:
        failures = managed.status.failure_count + 1
        now = self._wall_time()
        state = (
            "manual_action_required" if reason == "secret_unavailable" else "degraded"
        )
        managed.status = await self._store.save(
            managed.status.evolve(
                state=state,
                reason=reason,
                failure_count=failures,
                next_action_at=now + self._backoff(failures),
                updated_at=now,
            )
        )
        return self._render_status(managed.status)

    async def refresh_account(self, account_id: str) -> dict[str, object]:
        managed = self._accounts.get(account_id)
        if managed is None:
            raise KeyError(account_id)
        async with managed.lock:
            try:
                browser = await self._ensure_browser(managed)
                observation = await self._run_action(browser.probe())
                if observation.kind == "unauthenticated":
                    if await self._bootstrap_if_configured(managed, browser):
                        observation = await self._run_action(browser.probe())
                    if (
                        observation.kind == "unauthenticated"
                        and managed.config.login_mode == "email"
                    ):
                        observation = await self._email_login(managed, browser)
                        return await self._save_observation(
                            managed,
                            observation,
                            login_succeeded=observation.kind == "authenticated",
                            touched=observation.kind == "authenticated",
                            reason=(
                                None
                                if observation.kind == "authenticated"
                                else "email_login_failed"
                            ),
                        )
                return await self._save_observation(managed, observation)
            except ConfigurationError:
                return await self._save_failure(managed, "secret_unavailable")
            except TimeoutError:
                return await self._save_failure(managed, "action_timeout")
            except Exception as exc:
                logger.warning(
                    "keeper account refresh failed account=%s error=%s",
                    account_id,
                    type(exc).__name__,
                )
                return await self._save_failure(managed, "browser_error")

    async def touch_account(self, account_id: str) -> dict[str, object]:
        managed = self._accounts.get(account_id)
        if managed is None:
            raise KeyError(account_id)
        async with managed.lock:
            try:
                browser = await self._ensure_browser(managed)
                login_succeeded = False
                if managed.config.login_mode == "email":
                    observation = await self._email_login(managed, browser)
                    login_succeeded = observation.kind == "authenticated"
                    if observation.kind != "authenticated":
                        return await self._save_observation(
                            managed,
                            observation,
                            reason=(
                                None
                                if observation.kind == "challenge"
                                else "email_login_failed"
                            ),
                        )
                elif managed.config.bootstrap_token_file is not None:
                    await self._bootstrap_if_configured(managed, browser)

                observation = await self._run_action(browser.touch())
                return await self._save_observation(
                    managed,
                    observation,
                    login_succeeded=login_succeeded,
                    touched=observation.kind == "authenticated",
                    reason=(
                        "daily_checkin_claimed"
                        if observation.kind == "authenticated"
                        else None
                    ),
                )
            except ConfigurationError:
                return await self._save_failure(managed, "secret_unavailable")
            except TimeoutError:
                return await self._save_failure(managed, "action_timeout")
            except Exception as exc:
                logger.warning(
                    "keeper account touch failed account=%s error=%s",
                    account_id,
                    type(exc).__name__,
                )
                return await self._save_failure(managed, "browser_error")

    def _daily_touch_due(self, managed: _ManagedAccount, now: float) -> bool:
        local = datetime.fromtimestamp(
            now, resolve_timezone(managed.config.timezone_id)
        )
        if local.time().replace(tzinfo=None) < managed.config.daily_touch_time:
            return False
        return managed.status.last_touch_local_date != local.date().isoformat()

    async def run_due(self, now: float | None = None) -> None:
        async with self._due_lock:
            current = self._wall_time() if now is None else now
            actions = []
            for account_id, managed in self._accounts.items():
                status = managed.status
                if status.state in {
                    "challenge_required",
                    "manual_action_required",
                }:
                    continue
                if status.next_action_at > current:
                    continue
                if self._daily_touch_due(managed, current):
                    if managed.config.daily_checkin_enabled:
                        actions.append(self.touch_account(account_id))
                    continue
                elif (
                    status.last_probe_at is None
                    or status.last_probe_at + self.settings.probe_interval_seconds
                    <= current
                ):
                    actions.append(self.refresh_account(account_id))
            if actions:
                await asyncio.gather(*actions)

    async def _scheduler_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self.run_due()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("keeper scheduler failed error=%s", type(exc).__name__)
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self.settings.scheduler_interval_seconds,
                )
            except TimeoutError:
                pass

    async def refresh_all(self) -> list[dict[str, object]]:
        return list(
            await asyncio.gather(
                *(self.refresh_account(account_id) for account_id in self._accounts)
            )
        )

    @staticmethod
    def _iso_timestamp(value: float | None) -> str | None:
        if value is None:
            return None
        return datetime.fromtimestamp(value, timezone.utc).isoformat()

    @classmethod
    def _render_status(cls, status: AccountStatus) -> dict[str, object]:
        return {
            "id": status.account_id,
            "state": status.state,
            "reason": status.reason,
            "ready": status.state == "authenticated",
            "verify_http_status": status.verify_http_status,
            "userinfo_http_status": status.userinfo_http_status,
            "last_probe_at": cls._iso_timestamp(status.last_probe_at),
            "last_authenticated_at": cls._iso_timestamp(status.last_authenticated_at),
            "last_login_at": cls._iso_timestamp(status.last_login_at),
            "last_touch_at": cls._iso_timestamp(status.last_touch_at),
            "next_action_at": cls._iso_timestamp(
                status.next_action_at if status.next_action_at > 0 else None
            ),
        }

    def account_status(self) -> list[dict[str, object]]:
        return [
            self._render_status(self._accounts[account_id].status)
            for account_id in sorted(self._accounts)
        ]

    def is_ready(self) -> bool:
        return self._started and any(
            account.status.state == "authenticated"
            for account in self._accounts.values()
        )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._stop_event.set()
        if self._scheduler_task is not None:
            self._scheduler_task.cancel()
            await asyncio.gather(self._scheduler_task, return_exceptions=True)
            self._scheduler_task = None

        sessions = [
            managed.browser.close()
            for managed in self._accounts.values()
            if managed.browser is not None
        ]
        if sessions:
            await asyncio.gather(*sessions, return_exceptions=True)
        try:
            await self._runtime.close()
        except Exception as exc:
            logger.warning("keeper runtime close failed error=%s", type(exc).__name__)
        finally:
            now = self._wall_time()
            try:
                for managed in self._accounts.values():
                    managed.status = await self._store.save(
                        managed.status.evolve(
                            state="stopped", reason="shutdown", updated_at=now
                        )
                    )
            finally:
                await self._store.close()
