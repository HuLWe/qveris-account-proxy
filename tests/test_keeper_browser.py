from __future__ import annotations

from pathlib import Path

import pytest

from qveris_keeper.browser import (
    LOGIN_URL,
    PlaywrightAccountBrowser,
    build_launch_options,
)
from test_keeper_helpers import PROXY_PASSWORD, PROXY_USERNAME, make_account


def test_launch_options_pin_identity_profile_and_proxy(tmp_path: Path) -> None:
    account = make_account(tmp_path)

    first = build_launch_options(account)
    second = build_launch_options(account)

    assert first == second
    assert first["user_data_dir"] == str(account.profile_dir)
    assert first["locale"] == "en-US"
    assert first["timezone_id"] == "UTC"
    assert first["viewport"] == {"width": 1365, "height": 768}
    assert first["device_scale_factor"] == 1.0
    assert first["user_agent"] == account.user_agent
    assert first["proxy"] == {
        "server": "http://proxy.test:8080",
        "username": PROXY_USERNAME,
        "password": PROXY_PASSWORD,
    }
    assert account.profile_dir.is_dir()


def test_each_account_uses_a_distinct_persistent_profile(tmp_path: Path) -> None:
    first_account = make_account(tmp_path, account_id="account-a")
    second_account = make_account(tmp_path, account_id="account-b")

    first = build_launch_options(first_account)
    second = build_launch_options(second_account)

    assert first["user_data_dir"] != second["user_data_dir"]


class _FakePage:
    def __init__(self) -> None:
        self.url = "https://qveris.ai.example.test/redirect"
        self.goto_calls: list[str] = []
        self.evaluate_arguments: list[object] = []

    async def goto(self, url: str, **kwargs) -> None:
        self.goto_calls.append(url)
        self.url = url

    async def evaluate(self, script: str, argument=None) -> bool:
        self.evaluate_arguments.append(argument)
        return False


class _FakeContext:
    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_secret_injection_requires_exact_qveris_origin() -> None:
    page = _FakePage()
    browser = PlaywrightAccountBrowser(_FakeContext(), page)

    await browser.bootstrap_token("bootstrap-token-sentinel")

    assert page.goto_calls == [LOGIN_URL]
    assert page.evaluate_arguments == ["bootstrap-token-sentinel"]


class _FakeRpcPage(_FakePage):
    def __init__(self) -> None:
        super().__init__()
        self.url = LOGIN_URL
        self.rpc_script = ""

    async def evaluate(self, script: str, argument=None):
        if "document.querySelector" in script:
            return False
        self.rpc_script = script
        return {
            "verifyStatus": 200,
            "verifyOk": True,
            "userinfoStatus": 200,
            "userinfoOk": True,
            "networkError": False,
        }


@pytest.mark.asyncio
async def test_probe_checks_verify_and_userinfo_without_returning_payloads() -> None:
    page = _FakeRpcPage()
    browser = PlaywrightAccountBrowser(_FakeContext(), page)

    observation = await browser.probe()

    assert observation.kind == "authenticated"
    assert observation.verify_http_status == 200
    assert observation.userinfo_http_status == 200
    assert "/rpc/v1/auth/verify" in page.rpc_script
    assert "/rpc/v1/auth/userinfo" in page.rpc_script
