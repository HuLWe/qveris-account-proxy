from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from asgi_lifespan import LifespanManager

from qveris_keeper.app import create_app
from qveris_keeper.browser import SessionObservation
from test_keeper_helpers import (
    ADMIN_TOKEN,
    BOOTSTRAP_TOKEN,
    EMAIL_VALUE,
    PASSWORD_VALUE,
    PROXY_PASSWORD,
    PROXY_USERNAME,
    FakeAccountBrowser,
    FakeBrowserRuntime,
    MutableClock,
    make_account,
    make_settings,
)


NOW = datetime(2026, 1, 1, 12, tzinfo=timezone.utc).timestamp()


def auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {ADMIN_TOKEN}"}


@pytest.mark.asyncio
async def test_health_and_authenticated_admin_endpoints(tmp_path: Path) -> None:
    account = make_account(tmp_path)
    browser = FakeAccountBrowser(probes=[SessionObservation("authenticated", 200, 200)])
    runtime = FakeBrowserRuntime({account.id: browser})
    app = create_app(
        make_settings(tmp_path, (account,)),
        runtime=runtime,
        wall_time=MutableClock(NOW),
    )

    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://keeper.test"
        ) as client:
            live = await client.get("/health/live")
            ready = await client.get("/health/ready")
            denied = await client.get("/admin/v1/accounts")
            status = await client.get("/admin/v1/accounts", headers=auth_headers())
            refreshed = await client.post("/admin/v1/refresh", headers=auth_headers())
            touched = await client.post(
                f"/admin/v1/accounts/{account.id}/touch", headers=auth_headers()
            )

    assert live.status_code == 200
    assert ready.status_code == 200
    assert denied.status_code == 401
    assert status.status_code == 200
    assert status.headers["cache-control"] == "no-store"
    assert status.json()["accounts"][0]["state"] == "authenticated"
    assert refreshed.status_code == 200
    assert touched.status_code == 200
    assert runtime.closed is True


@pytest.mark.asyncio
async def test_degraded_readiness_and_unknown_account(tmp_path: Path) -> None:
    account = make_account(tmp_path, login_mode="manual")
    browser = FakeAccountBrowser(probes=[SessionObservation("unauthenticated", 401)])
    app = create_app(
        make_settings(tmp_path, (account,)),
        runtime=FakeBrowserRuntime({account.id: browser}),
        wall_time=MutableClock(NOW),
    )

    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://keeper.test"
        ) as client:
            ready = await client.get("/health/ready")
            missing = await client.post(
                "/admin/v1/accounts/unknown/refresh", headers=auth_headers()
            )

    assert ready.status_code == 503
    assert ready.json() == {"status": "degraded"}
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_admin_responses_exclude_all_secret_material(tmp_path: Path) -> None:
    account = make_account(tmp_path, bootstrap_token=True)
    browser = FakeAccountBrowser(probes=[SessionObservation("authenticated", 200, 200)])
    app = create_app(
        make_settings(tmp_path, (account,)),
        runtime=FakeBrowserRuntime({account.id: browser}),
        wall_time=MutableClock(NOW),
    )

    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://keeper.test"
        ) as client:
            response = await client.get("/admin/v1/accounts", headers=auth_headers())

    rendered = response.text
    for secret in (
        ADMIN_TOKEN,
        EMAIL_VALUE,
        PASSWORD_VALUE,
        BOOTSTRAP_TOKEN,
        PROXY_USERNAME,
        PROXY_PASSWORD,
    ):
        assert secret not in rendered


@pytest.mark.asyncio
async def test_openapi_and_docs_are_disabled(tmp_path: Path) -> None:
    account = make_account(tmp_path, login_mode="manual")
    browser = FakeAccountBrowser(probes=[SessionObservation("unauthenticated", 401)])
    app = create_app(
        make_settings(tmp_path, (account,)),
        runtime=FakeBrowserRuntime({account.id: browser}),
        wall_time=MutableClock(NOW),
    )

    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://keeper.test"
        ) as client:
            assert (await client.get("/openapi.json")).status_code == 404
            assert (await client.get("/docs")).status_code == 404
