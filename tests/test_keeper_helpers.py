from __future__ import annotations

import asyncio
from collections import deque
from datetime import time
from pathlib import Path

from qveris_keeper.browser import SessionObservation
from qveris_keeper.config import (
    BrowserAccountConfig,
    KeeperSettings,
    ProxyFileConfig,
    ViewportConfig,
)

ADMIN_TOKEN = "keeper-admin-token-sentinel-value"
EMAIL_VALUE = "keeper-user-sentinel@example.test"
PASSWORD_VALUE = "keeper-password-sentinel-value"
BOOTSTRAP_TOKEN = "keeper-bootstrap-token-sentinel-value"
PROXY_USERNAME = "keeper-proxy-user-sentinel"
PROXY_PASSWORD = "keeper-proxy-password-sentinel"


class MutableClock:
    def __init__(self, value: float) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


class FakeAccountBrowser:
    def __init__(
        self,
        *,
        probes: list[SessionObservation] | None = None,
        logins: list[SessionObservation] | None = None,
        touches: list[SessionObservation] | None = None,
        delay: float = 0,
    ) -> None:
        self.probes = deque(probes or [SessionObservation("unauthenticated", 401)])
        self.logins = deque(logins or [SessionObservation("authenticated", 200, 200)])
        self.touches = deque(touches or [SessionObservation("authenticated", 200, 200)])
        self.delay = delay
        self.probe_calls = 0
        self.login_calls = 0
        self.touch_calls = 0
        self.bootstrap_calls = 0
        self.closed = False
        self.received_email: str | None = None
        self.received_password: str | None = None
        self.received_token: str | None = None
        self.active = 0
        self.max_active = 0

    @staticmethod
    def _take(queue: deque[SessionObservation]) -> SessionObservation:
        value = queue[0]
        if len(queue) > 1:
            queue.popleft()
        return value

    async def _enter(self) -> None:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        if self.delay:
            await asyncio.sleep(self.delay)

    def _exit(self) -> None:
        self.active -= 1

    async def bootstrap_token(self, token: str) -> None:
        await self._enter()
        try:
            self.bootstrap_calls += 1
            self.received_token = token
        finally:
            self._exit()

    async def login_email(self, email: str, password: str) -> SessionObservation:
        await self._enter()
        try:
            self.login_calls += 1
            self.received_email = email
            self.received_password = password
            return self._take(self.logins)
        finally:
            self._exit()

    async def probe(self) -> SessionObservation:
        await self._enter()
        try:
            self.probe_calls += 1
            return self._take(self.probes)
        finally:
            self._exit()

    async def touch(self) -> SessionObservation:
        await self._enter()
        try:
            self.touch_calls += 1
            return self._take(self.touches)
        finally:
            self._exit()

    async def close(self) -> None:
        self.closed = True


class FakeBrowserRuntime:
    def __init__(self, sessions: dict[str, FakeAccountBrowser]) -> None:
        self.sessions = sessions
        self.opened: list[BrowserAccountConfig] = []
        self.closed = False

    async def open(self, account: BrowserAccountConfig) -> FakeAccountBrowser:
        self.opened.append(account)
        return self.sessions[account.id]

    async def close(self) -> None:
        self.closed = True


def write_secret(path: Path, value: str) -> Path:
    path.write_text(value, encoding="utf-8")
    return path


def make_account(
    tmp_path: Path,
    *,
    account_id: str = "account-a",
    login_mode: str = "email",
    bootstrap_token: bool = False,
    daily_touch_time: time = time(23, 59),
) -> BrowserAccountConfig:
    secret_dir = tmp_path / "secrets" / account_id
    secret_dir.mkdir(parents=True, exist_ok=True)
    proxy_server = write_secret(secret_dir / "proxy-server", "http://proxy.test:8080")
    proxy_user = write_secret(secret_dir / "proxy-user", PROXY_USERNAME)
    proxy_password = write_secret(secret_dir / "proxy-password", PROXY_PASSWORD)
    payload = {
        "id": account_id,
        "login_mode": login_mode,
        "profile_dir": tmp_path / "profiles" / account_id,
        "proxy": ProxyFileConfig(
            server_file=proxy_server,
            username_file=proxy_user,
            password_file=proxy_password,
        ),
        "locale": "en-US",
        "timezone_id": "UTC",
        "viewport": ViewportConfig(width=1365, height=768),
        "user_agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "Chrome/138.0.0.0 Safari/537.36"
        ),
        "daily_touch_time": daily_touch_time,
    }
    if login_mode == "email":
        payload["email_file"] = write_secret(secret_dir / "email", EMAIL_VALUE)
        payload["password_file"] = write_secret(secret_dir / "password", PASSWORD_VALUE)
    if bootstrap_token:
        payload["bootstrap_token_file"] = write_secret(
            secret_dir / "bootstrap-token", BOOTSTRAP_TOKEN
        )
    return BrowserAccountConfig.model_validate(payload)


def make_settings(
    tmp_path: Path,
    accounts: tuple[BrowserAccountConfig, ...],
    **overrides,
) -> KeeperSettings:
    values = {
        "accounts": accounts,
        "admin_token": ADMIN_TOKEN,
        "state_path": str(tmp_path / "runtime" / "keeper.db"),
        "profile_root": tmp_path / "profiles",
        "probe_interval_seconds": 900,
        "scheduler_interval_seconds": 3600,
        "action_timeout_seconds": 5,
        "retry_base_seconds": 1,
        "retry_max_seconds": 60,
    }
    values.update(overrides)
    return KeeperSettings.model_validate(values)
