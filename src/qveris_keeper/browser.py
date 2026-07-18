from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

from .config import BrowserAccountConfig, read_proxy_options

QVERIS_ORIGIN = "https://qveris.ai"
LOGIN_URL = f"{QVERIS_ORIGIN}/login"
ACCOUNT_URL = f"{QVERIS_ORIGIN}/account?page=overview"

ObservationKind = Literal[
    "authenticated", "unauthenticated", "challenge", "transient_error"
]


@dataclass(frozen=True, slots=True)
class SessionObservation:
    kind: ObservationKind
    verify_http_status: int = 0
    userinfo_http_status: int = 0


class AccountBrowser(Protocol):
    async def bootstrap_token(self, token: str) -> None: ...

    async def login_email(self, email: str, password: str) -> SessionObservation: ...

    async def probe(self) -> SessionObservation: ...

    async def touch(self) -> SessionObservation: ...

    async def close(self) -> None: ...


class BrowserRuntime(Protocol):
    async def open(self, account: BrowserAccountConfig) -> AccountBrowser: ...

    async def close(self) -> None: ...


def build_launch_options(account: BrowserAccountConfig) -> dict[str, Any]:
    """Build immutable per-account launch options without logging their values."""
    account.profile_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        account.profile_dir.chmod(0o700)
    except OSError:
        pass
    return {
        "user_data_dir": str(account.profile_dir),
        "headless": account.headless,
        "locale": account.locale,
        "timezone_id": account.timezone_id,
        "viewport": {
            "width": account.viewport.width,
            "height": account.viewport.height,
        },
        "device_scale_factor": account.viewport.device_scale_factor,
        "user_agent": account.user_agent,
        "proxy": read_proxy_options(account.proxy),
        "args": [
            f"--window-size={account.viewport.width},{account.viewport.height}",
            "--disable-features=PasswordManagerOnboarding,AutofillServerCommunication",
            "--disable-save-password-bubble",
        ],
    }


class PlaywrightBrowserRuntime:
    def __init__(self) -> None:
        self._playwright: Any | None = None
        self._sessions: set[PlaywrightAccountBrowser] = set()
        self._lock = asyncio.Lock()

    async def _ensure_started(self) -> Any:
        async with self._lock:
            if self._playwright is None:
                from playwright.async_api import async_playwright

                self._playwright = await async_playwright().start()
            return self._playwright

    async def open(self, account: BrowserAccountConfig) -> AccountBrowser:
        playwright = await self._ensure_started()
        options = build_launch_options(account)
        user_data_dir = options.pop("user_data_dir")
        context = await playwright.chromium.launch_persistent_context(
            user_data_dir, **options
        )
        page = context.pages[0] if context.pages else await context.new_page()
        session = PlaywrightAccountBrowser(context, page)
        self._sessions.add(session)
        try:
            await session.initialize()
        except Exception:
            self._sessions.discard(session)
            await context.close()
            raise
        return session

    async def close(self) -> None:
        sessions = tuple(self._sessions)
        self._sessions.clear()
        if sessions:
            await asyncio.gather(
                *(session.close() for session in sessions), return_exceptions=True
            )
        async with self._lock:
            if self._playwright is not None:
                playwright = self._playwright
                self._playwright = None
                await playwright.stop()


class PlaywrightAccountBrowser:
    _PROBE_SCRIPT = """
    async () => {
      const result = {
        hasToken: false,
        verifyStatus: 0,
        verifyOk: false,
        userinfoStatus: 0,
        userinfoOk: false,
        networkError: false
      };
      let token = null;
      try {
        const persisted = JSON.parse(localStorage.getItem("auth-storage") || "{}");
        token = persisted && persisted.state && persisted.state.token;
        if (!token) token = localStorage.getItem("token");
      } catch (_) {}
      if (typeof token !== "string" || token.length === 0) return result;
      result.hasToken = true;
      const headers = {Accept: "application/json", Authorization: `Bearer ${token}`};
      try {
        const verify = await fetch("/rpc/v1/auth/verify", {
          method: "GET", cache: "no-store", credentials: "include", headers
        });
        result.verifyStatus = verify.status;
        const verifyBody = await verify.json().catch(() => ({}));
        result.verifyOk = verify.ok &&
          (verifyBody.success === true || verifyBody.status === "success");
        const info = await fetch("/rpc/v1/auth/userinfo", {
          method: "GET", cache: "no-store", credentials: "include", headers
        });
        result.userinfoStatus = info.status;
        const infoBody = await info.json().catch(() => ({}));
        result.userinfoOk = info.ok && infoBody.status === "success";
        return result;
      } catch (_) {
        result.networkError = true;
        return result;
      }
    }
    """

    _LOGIN_SCRIPT = """
    async ({email, password}) => {
      const result = {status: 0, stored: false, challenge: false, networkError: false};
      try {
        const response = await fetch("/rpc/v1/auth/login", {
          method: "POST",
          cache: "no-store",
          credentials: "include",
          headers: {Accept: "application/json", "Content-Type": "application/json"},
          body: JSON.stringify({email, username: email, password})
        });
        result.status = response.status;
        const body = await response.json().catch(() => ({}));
        const token = body.token || (body.data && body.data.access_token);
        const marker = JSON.stringify(body).toLowerCase();
        result.challenge = response.status === 403 &&
          (marker.includes("turnstile") || marker.includes("captcha") ||
           marker.includes("challenge"));
        if (response.ok && typeof token === "string" && token.length > 0) {
          const previous = JSON.parse(localStorage.getItem("auth-storage") || "{}");
          const state = Object.assign({}, previous.state || {}, {
            token, user: null, isAuthenticated: true
          });
          localStorage.setItem("auth-storage", JSON.stringify({
            state, version: Number.isInteger(previous.version) ? previous.version : 0
          }));
          document.cookie = "qveris_auth_hint=1; path=/; max-age=86400; SameSite=Lax";
          result.stored = true;
        }
        return result;
      } catch (_) {
        result.networkError = true;
        return result;
      }
    }
    """

    _BOOTSTRAP_SCRIPT = """
    token => {
      const previous = JSON.parse(localStorage.getItem("auth-storage") || "{}");
      const state = Object.assign({}, previous.state || {}, {
        token, user: null, isAuthenticated: true
      });
      localStorage.setItem("auth-storage", JSON.stringify({
        state, version: Number.isInteger(previous.version) ? previous.version : 0
      }));
      document.cookie = "qveris_auth_hint=1; path=/; max-age=86400; SameSite=Lax";
    }
    """

    _CHALLENGE_SCRIPT = """
    () => Boolean(document.querySelector(
      'iframe[src*="challenges.cloudflare.com"], ' +
      'input[name="cf-turnstile-response"], [data-sitekey]'
    ))
    """

    def __init__(self, context: Any, page: Any) -> None:
        self._context = context
        self._page = page
        self._closed = False
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        await self._page.goto(LOGIN_URL, wait_until="domcontentloaded")

    async def _ensure_origin(self) -> None:
        current = urlsplit(str(self._page.url))
        if current.scheme != "https" or current.netloc != "qveris.ai":
            await self._page.goto(LOGIN_URL, wait_until="domcontentloaded")

    async def _challenge_visible(self) -> bool:
        try:
            return bool(await self._page.evaluate(self._CHALLENGE_SCRIPT))
        except Exception:
            return False

    @staticmethod
    def _classify_probe(result: dict[str, Any], challenge: bool) -> SessionObservation:
        verify_status = int(result.get("verifyStatus") or 0)
        userinfo_status = int(result.get("userinfoStatus") or 0)
        if challenge:
            return SessionObservation("challenge", verify_status, userinfo_status)
        if result.get("networkError"):
            return SessionObservation("transient_error", verify_status, userinfo_status)
        if result.get("verifyOk") and result.get("userinfoOk"):
            return SessionObservation("authenticated", verify_status, userinfo_status)
        if verify_status == 429 or verify_status >= 500 or userinfo_status >= 500:
            return SessionObservation("transient_error", verify_status, userinfo_status)
        return SessionObservation("unauthenticated", verify_status, userinfo_status)

    async def bootstrap_token(self, token: str) -> None:
        async with self._lock:
            await self._ensure_origin()
            await self._page.evaluate(self._BOOTSTRAP_SCRIPT, token)

    async def login_email(self, email: str, password: str) -> SessionObservation:
        async with self._lock:
            await self._ensure_origin()
            result = await self._page.evaluate(
                self._LOGIN_SCRIPT, {"email": email, "password": password}
            )
            if result.get("networkError"):
                return SessionObservation("transient_error")
            if result.get("challenge") or await self._challenge_visible():
                return SessionObservation("challenge", int(result.get("status") or 0))
            if not result.get("stored"):
                return SessionObservation(
                    "unauthenticated", int(result.get("status") or 0)
                )
            probe = await self._page.evaluate(self._PROBE_SCRIPT)
            return self._classify_probe(probe, await self._challenge_visible())

    async def probe(self) -> SessionObservation:
        async with self._lock:
            await self._ensure_origin()
            result = await self._page.evaluate(self._PROBE_SCRIPT)
            return self._classify_probe(result, await self._challenge_visible())

    async def touch(self) -> SessionObservation:
        async with self._lock:
            await self._page.goto(ACCOUNT_URL, wait_until="domcontentloaded")
            result = await self._page.evaluate(self._PROBE_SCRIPT)
            return self._classify_probe(result, await self._challenge_visible())

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._context.close()
