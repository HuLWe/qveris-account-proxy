from __future__ import annotations

import asyncio
import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest
from asgi_lifespan import LifespanManager

from qveris_proxy.admin import serialize_accounts
from qveris_proxy.app import create_app
from qveris_proxy.bootstrap import (
    ADMIN_BROWSER_SESSION_COOKIE,
    ADMIN_BROWSER_SESSION_HEADER,
    AdminBrowserSessions,
    AdminBootstrapTickets,
)
from qveris_proxy.routes import PUBLIC_OPERATIONS, QVERIS_API_VERSION
from qveris_proxy.state import StoredCooldown
from conftest import (
    ACCESS_TOKEN,
    KEY_A1,
    KEY_A2,
    OAUTH_A1,
    make_settings,
)


def auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {ACCESS_TOKEN}"}


async def app_client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://proxy.test",
    )


class _BlockingAdminResponseStream(httpx.AsyncByteStream):
    def __init__(self, started: asyncio.Event, release: asyncio.Event) -> None:
        self._started = started
        self._release = release

    async def __aiter__(self):
        self._started.set()
        yield b'{"search_id":"deleted-'
        await self._release.wait()
        yield b'stream"}'


def editable_payload(*, weight: int = 1, name: str = "主账号") -> dict[str, object]:
    return {
        "accounts": [
            {
                "id": "account-a",
                "name": name,
                "weight": weight,
                "requests_per_minute": 10_000,
                "burst": 10_000,
                "transport": {
                    "user_agent": "qveris-account-proxy/0.1.0",
                    "accept_language": "en-US,en;q=0.9",
                    "proxy_url_file": None,
                },
                "keys": [
                    {"id": "primary", "value": None},
                    {"id": "standby", "value": None},
                ],
                "oauth_tokens": [{"id": "primary", "value": None}],
            }
        ]
    }


@pytest.mark.asyncio
async def test_admin_shell_and_assets_are_static_and_hardened() -> None:
    calls = 0

    async def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    app = create_app(make_settings(), transport=httpx.MockTransport(upstream))
    async with LifespanManager(app):
        async with await app_client(app) as client:
            redirect = await client.get("/admin", follow_redirects=False)
            shell = await client.get("/admin/")
            script = await client.get("/admin/assets/admin.js")
            stylesheet = await client.get("/admin/assets/admin.css")
            cached = await client.get(
                "/admin/assets/admin.js",
                headers={"If-None-Match": script.headers["etag"]},
            )
            missing = await client.get("/admin/assets/unknown.js")

    assert redirect.status_code == 307
    assert redirect.headers["location"] == "/admin/"
    assert shell.status_code == 200
    assert shell.headers["cache-control"] == "no-store"
    assert b"__ADMIN_CSS_VERSION__" not in shell.content
    assert b"__ADMIN_JS_VERSION__" not in shell.content
    assert re.search(rb"admin\.css\?v=[0-9a-f]{64}", shell.content)
    assert re.search(rb"admin\.js\?v=[0-9a-f]{64}", shell.content)
    assert "default-src 'none'" in shell.headers["content-security-policy"]
    assert shell.headers["x-content-type-options"] == "nosniff"
    assert script.status_code == 200
    assert script.headers["content-type"].startswith("text/javascript")
    assert script.headers["cache-control"] == "no-cache"
    assert stylesheet.status_code == 200
    assert stylesheet.headers["content-type"].startswith("text/css")
    assert stylesheet.headers["cache-control"] == "no-cache"
    assert cached.status_code == 304
    assert missing.status_code == 404
    combined = shell.content + script.content
    assert ACCESS_TOKEN.encode() not in combined
    assert KEY_A1.encode() not in combined
    assert KEY_A2.encode() not in combined
    assert OAUTH_A1.encode() not in combined
    assert b"localStorage" not in script.content
    assert b"window.sessionStorage.setItem" in script.content
    assert b"window.sessionStorage.removeItem" in script.content
    assert b"claimFirstBrowserSession" in script.content
    assert b"resumeBrowserSession" in script.content
    assert b"rememberBrowserSession" in script.content
    assert b"forgetBrowserSession" in script.content
    assert b"window.history.replaceState" in script.content
    assert b"window.location.hash" in script.content
    assert b'cleanUrl.searchParams.delete("launch")' in script.content
    assert b"resetWorkspace" in script.content
    assert b"window.navigator.clipboard.writeText" in script.content
    assert b'document.execCommand("copy")' in script.content
    assert b"innerHTML" not in script.content
    assert b"eval(" not in script.content
    assert b"https://qveris.ai/?ref=afAfj_c90cnWYg" in shell.content
    assert shell.content.count(b"https://qveris.ai/?ref=afAfj_c90cnWYg") == 2
    assert b"75gxF1vtvXWj_A" in shell.content
    assert b'rel="noopener noreferrer"' in shell.content
    assert b'id="register-account"' in shell.content
    assert b'class="onboarding-actions"' in shell.content
    assert b'id="copy-api-key"' in shell.content
    assert b'id="api-key-display"' in shell.content
    assert b'id="toggle-api-key"' in shell.content
    assert b'id="api-base-url"' in shell.content
    assert b'id="copy-base-url"' in shell.content
    assert b'id="copy-connection"' in shell.content
    assert b'id="pool-summary"' in shell.content
    assert b'data-tab="proxy-keys"' in shell.content
    assert b'id="proxy-keys"' in shell.content
    assert b'id="create-proxy-key"' in shell.content
    assert b'id="proxy-key-editor"' in shell.content
    assert b'id="proxy-key-secret-dialog"' in shell.content
    assert b'id="created-proxy-key"' in shell.content
    assert b'"/admin/v1/proxy-keys"' in script.content
    assert b"showCreatedProxySecret" in script.content
    assert b"clearCreatedProxySecret" in script.content
    assert b"proxyKeyErrorMessage" in script.content
    assert b'remove.disabled = busy || key.kind === "primary"' in script.content
    assert "默认代理 Key 为系统保留，不可删除".encode() in script.content
    assert b'if (key.kind === "primary")' in script.content
    assert b".key-dialog" in stylesheet.content
    assert 'aria-label="接入应用"'.encode() in shell.content
    assert 'aria-label="复制全部接入配置"'.encode() in shell.content
    assert 'aria-label="复制 API Base URL"'.encode() in shell.content
    assert 'aria-label="复制代理 API Key"'.encode() in shell.content
    assert b'id="topbar-actions" hidden' in shell.content
    assert b'id="manual-connect"' in shell.content
    assert b">\xe5\xa4\x8d\xe5\x88\xb6</button>" in shell.content
    assert b"window.crypto.getRandomValues" in script.content
    assert b"window.confirm" in script.content
    assert b'method: "DELETE"' in script.content
    assert b"Promise.allSettled" in script.content
    assert b"deletingAccountId" in script.content
    assert b"account.persisted && account.id === accountId" in script.content
    assert b"function editAccount" in script.content
    assert b'activateTab("config")' in script.content
    assert b"editButton.dataset.accountEdit" in script.content
    assert "账号名称".encode() in script.content
    assert "内部 ID".encode() in script.content
    assert b"nextAccountIdentity" in script.content
    assert b"actionGroup.append(testButton, editButton, deleteButton)" in script.content
    assert b"deleteButton.disabled = deleteInProgress" in script.content
    assert b"remove.disabled = deleteInProgress" in script.content
    assert "请先添加并保存另一个账号".encode() in script.content
    assert "稳定连接标识".encode() in script.content
    assert b"[hidden]" in stylesheet.content
    assert b"display: none !important" in stylesheet.content
    assert b".manual-connect:not([open]) > .manual-auth" in stylesheet.content
    assert b"button.danger:hover:not(:disabled)" in stylesheet.content
    assert b".onboarding-actions" in stylesheet.content
    assert b".registration-link-secondary" in stylesheet.content
    assert b".account-editor.edit-target" in stylesheet.content
    assert calls == 0


def test_admin_browser_session_is_signed_secret_free_and_expiring() -> None:
    now = [1_700_000_000.0]
    sessions = AdminBrowserSessions(
        ACCESS_TOKEN,
        max_age_seconds=60,
        wall_time=lambda: now[0],
    )
    session = sessions.issue()

    assert sessions.validate(session)
    assert ACCESS_TOKEN not in session
    assert re.fullmatch(r"[0-9a-f]{64}", sessions.claim_key)

    replacement = "A" if session[-1] != "A" else "B"
    assert not sessions.validate(f"{session[:-1]}{replacement}")
    assert not AdminBrowserSessions(
        f"{ACCESS_TOKEN}-rotated",
        max_age_seconds=60,
        wall_time=lambda: now[0],
    ).validate(session)

    now[0] += 60
    assert not sessions.validate(session)


@pytest.mark.asyncio
async def test_first_open_browser_claim_is_atomic_and_persists_across_restart(
    tmp_path: Path,
) -> None:
    state_path = str(tmp_path / "first-open.db")
    settings = make_settings(
        admin_first_open_claim_enabled=True,
        state_path=state_path,
    )
    header = {ADMIN_BROWSER_SESSION_HEADER: "1"}
    app = create_app(settings, transport=httpx.MockTransport(lambda _: None))

    async with LifespanManager(app):
        async with await app_client(app) as first, await app_client(app) as second:
            missing_header = await first.post("/admin/v1/browser-session/claim")
            claimed, raced = await asyncio.gather(
                first.post("/admin/v1/browser-session/claim", headers=header),
                second.post("/admin/v1/browser-session/claim", headers=header),
            )
            winner = claimed if claimed.status_code == 200 else raced
            session_cookie = winner.cookies.get(ADMIN_BROWSER_SESSION_COOKIE)

    assert missing_header.status_code == 400
    assert sorted((claimed.status_code, raced.status_code)) == [200, 409]
    assert winner.json() == {"access_token": ACCESS_TOKEN}
    assert session_cookie
    assert ACCESS_TOKEN not in session_cookie
    set_cookie = winner.headers["set-cookie"]
    assert "HttpOnly" in set_cookie
    assert "Max-Age=15552000" in set_cookie
    assert "Path=/admin" in set_cookie
    assert "SameSite=strict" in set_cookie
    assert winner.headers["cache-control"] == "no-store"

    restarted = create_app(
        settings,
        transport=httpx.MockTransport(lambda _: None),
    )
    async with LifespanManager(restarted):
        async with await app_client(restarted) as client:
            resumed = await client.get(
                "/admin/v1/browser-session",
                headers={
                    **header,
                    "Cookie": f"{ADMIN_BROWSER_SESSION_COOKIE}={session_cookie}",
                },
            )
            reclaimed = await client.post(
                "/admin/v1/browser-session/claim", headers=header
            )

    assert resumed.status_code == 200
    assert resumed.json() == {"access_token": ACCESS_TOKEN}
    assert reclaimed.status_code == 409


@pytest.mark.asyncio
async def test_authenticated_browser_session_can_resume_and_disconnect() -> None:
    app = create_app(make_settings(), transport=httpx.MockTransport(lambda _: None))
    header = {ADMIN_BROWSER_SESSION_HEADER: "1"}

    async with LifespanManager(app):
        async with await app_client(app) as client:
            disabled_claim = await client.post(
                "/admin/v1/browser-session/claim", headers=header
            )
            denied = await client.post("/admin/v1/browser-session")
            remembered = await client.post(
                "/admin/v1/browser-session", headers=auth_headers()
            )
            resumed = await client.get("/admin/v1/browser-session", headers=header)
            disconnected = await client.delete("/admin/v1/browser-session")
            expired = await client.get("/admin/v1/browser-session", headers=header)

    assert disabled_claim.status_code == 403
    assert denied.status_code == 401
    assert remembered.status_code == 200
    assert resumed.status_code == 200
    assert resumed.json() == {"access_token": ACCESS_TOKEN}
    assert disconnected.status_code == 200
    assert disconnected.json() == {"disconnected": True}
    assert expired.status_code == 401


@pytest.mark.asyncio
async def test_admin_bootstrap_ticket_is_authenticated_short_lived_and_one_time() -> (
    None
):
    app = create_app(make_settings(), transport=httpx.MockTransport(lambda _: None))
    async with LifespanManager(app):
        async with await app_client(app) as client:
            bootstrap_headers = {"X-QVeris-Bootstrap": "1"}
            denied = await client.post("/admin/v1/bootstrap-ticket")
            issued = await client.post(
                "/admin/v1/bootstrap-ticket", headers=auth_headers()
            )
            ticket = issued.json()["ticket"]
            missing_header = await client.post(
                "/admin/v1/bootstrap/exchange", json={"ticket": ticket}
            )
            wrong = await client.post(
                "/admin/v1/bootstrap/exchange",
                json={"ticket": "x" * 43},
                headers=bootstrap_headers,
            )
            oversized = await client.post(
                "/admin/v1/bootstrap/exchange",
                content=b"x" * 513,
                headers={
                    "Content-Type": "application/json",
                    **bootstrap_headers,
                },
            )
            exchanged = await client.post(
                "/admin/v1/bootstrap/exchange",
                json={"ticket": ticket},
                headers=bootstrap_headers,
            )
            replayed = await client.post(
                "/admin/v1/bootstrap/exchange",
                json={"ticket": ticket},
                headers=bootstrap_headers,
            )

    assert denied.status_code == 401
    assert issued.status_code == 200
    assert issued.headers["cache-control"] == "no-store"
    assert issued.json()["expires_in"] == 60
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", ticket)
    assert ticket != ACCESS_TOKEN
    assert missing_header.status_code == 400
    assert wrong.status_code == 401
    assert wrong.headers["cache-control"] == "no-store"
    assert oversized.status_code == 413
    assert exchanged.status_code == 200
    assert exchanged.headers["cache-control"] == "no-store"
    assert exchanged.headers["pragma"] == "no-cache"
    assert exchanged.json() == {"access_token": ACCESS_TOKEN}
    assert replayed.status_code == 401


def test_admin_bootstrap_ticket_expires() -> None:
    now = [100.0]
    tickets = AdminBootstrapTickets(ttl_seconds=1, clock=lambda: now[0])
    ticket = tickets.issue()

    now[0] = 101.0

    assert not tickets.consume(ticket)


def test_admin_bootstrap_ticket_has_one_concurrent_consumer() -> None:
    tickets = AdminBootstrapTickets()
    ticket = tickets.issue()

    with ThreadPoolExecutor(max_workers=8) as executor:
        consumed = list(executor.map(tickets.consume, [ticket] * 8))

    assert consumed.count(True) == 1
    assert consumed.count(False) == 7


def test_admin_bootstrap_ticket_capacity_and_clear() -> None:
    tickets = AdminBootstrapTickets(max_pending=1)
    first = tickets.issue()

    with pytest.raises(RuntimeError, match="capacity"):
        tickets.issue()

    tickets.clear()
    second = tickets.issue()

    assert not tickets.consume(first)
    assert tickets.consume(second)


@pytest.mark.asyncio
async def test_admin_config_and_operation_catalog_require_auth_and_redact() -> None:
    app = create_app(make_settings(), transport=httpx.MockTransport(lambda _: None))
    async with LifespanManager(app):
        async with await app_client(app) as client:
            denied_config = await client.get("/admin/v1/config")
            denied_catalog = await client.get("/admin/v1/operations")
            config = await client.get("/admin/v1/config", headers=auth_headers())
            catalog = await client.get("/admin/v1/operations", headers=auth_headers())

    assert denied_config.status_code == 401
    assert denied_catalog.status_code == 401
    assert config.status_code == 200
    assert config.headers["cache-control"] == "no-store"
    payload = config.json()
    assert payload["capabilities"]["persistent_editing"] is False
    assert payload["accounts"][0]["name"] == "account-a"
    assert payload["accounts"][0]["keys"] == [
        {"id": "primary", "configured": True},
        {"id": "standby", "configured": True},
    ]
    rendered = config.text
    assert ACCESS_TOKEN not in rendered
    assert KEY_A1 not in rendered
    assert KEY_A2 not in rendered
    assert OAUTH_A1 not in rendered
    assert "accounts_file_path" not in rendered
    assert "state_path" not in rendered
    assert catalog.status_code == 200
    assert catalog.json()["api_version"] == QVERIS_API_VERSION
    operations = catalog.json()["operations"]
    assert len(operations) == len(PUBLIC_OPERATIONS) == 19
    assert {(item["method"], item["path"]) for item in operations} == set(
        PUBLIC_OPERATIONS
    )


@pytest.mark.asyncio
async def test_account_status_reports_authoritative_management_actions(
    tmp_path: Path,
) -> None:
    async def status_for(settings) -> list[dict[str, object]]:
        app = create_app(settings, transport=httpx.MockTransport(lambda _: None))
        async with LifespanManager(app):
            async with await app_client(app) as client:
                response = await client.get(
                    "/admin/v1/accounts", headers=auth_headers()
                )
        assert response.status_code == 200
        return response.json()["accounts"]

    read_only = await status_for(make_settings())
    assert read_only[0]["name"] == "account-a"
    assert read_only[0]["management"] == {
        "can_edit": False,
        "edit_reason": "persistent_editing_disabled",
        "can_delete": False,
        "delete_reason": "persistent_editing_disabled",
    }

    single_path = tmp_path / "single.json"
    single_settings = make_settings(
        accounts_file_path=str(single_path),
        accounts_reload_interval_seconds=0,
        config_write_enabled=True,
    )
    single_path.write_bytes(serialize_accounts(single_settings.accounts))
    single = await status_for(single_settings)
    assert single[0]["management"]["can_edit"] is True
    assert single[0]["management"]["can_delete"] is False
    assert single[0]["management"]["delete_reason"] == "last_account_required"

    multiple_path = tmp_path / "multiple.json"
    multiple_settings = make_settings(
        multiple_accounts=True,
        default_account="account-b",
        accounts_file_path=str(multiple_path),
        accounts_reload_interval_seconds=0,
        config_write_enabled=True,
    )
    multiple_path.write_bytes(serialize_accounts(multiple_settings.accounts))
    multiple = await status_for(multiple_settings)
    management = {item["id"]: item["management"] for item in multiple}
    assert management["account-a"]["can_delete"] is True
    assert management["account-b"]["can_edit"] is True
    assert management["account-b"]["can_delete"] is False
    assert management["account-b"]["delete_reason"] == "default_account_locked"


@pytest.mark.asyncio
async def test_validation_preserves_secrets_and_does_not_apply() -> None:
    app = create_app(make_settings(), transport=httpx.MockTransport(lambda _: None))
    submitted = editable_payload(weight=7)
    async with LifespanManager(app):
        async with await app_client(app) as client:
            validated = await client.post(
                "/admin/v1/config/validate",
                headers=auth_headers(),
                json=submitted,
            )
            config = await client.get("/admin/v1/config", headers=auth_headers())

    assert validated.status_code == 200
    assert validated.json() == {
        "valid": True,
        "changed": True,
        "account_count": 1,
        "api_key_count": 2,
        "oauth_token_count": 1,
    }
    assert config.json()["accounts"][0]["weight"] == 1
    assert KEY_A1 not in validated.text
    assert KEY_A2 not in validated.text
    assert OAUTH_A1 not in validated.text


@pytest.mark.asyncio
async def test_validation_error_never_echoes_submitted_secret() -> None:
    submitted_secret = "sentinel-new-secret-never-render"
    payload = editable_payload()
    payload["accounts"][0]["keys"].append({"id": "bad id", "value": submitted_secret})
    app = create_app(make_settings(), transport=httpx.MockTransport(lambda _: None))
    async with LifespanManager(app):
        async with await app_client(app) as client:
            response = await client.post(
                "/admin/v1/config/validate",
                headers=auth_headers(),
                json=payload,
            )

    assert response.status_code == 400
    assert response.json() == {"detail": "invalid_config"}
    assert submitted_secret not in response.text


@pytest.mark.asyncio
async def test_persistent_editing_is_opt_in() -> None:
    app = create_app(make_settings(), transport=httpx.MockTransport(lambda _: None))
    async with LifespanManager(app):
        async with await app_client(app) as client:
            response = await client.put(
                "/admin/v1/config",
                headers=auth_headers(),
                json=editable_payload(weight=2),
            )

    assert response.status_code == 403
    assert response.json() == {"detail": "persistent_editing_disabled"}


@pytest.mark.asyncio
async def test_persistent_editing_preserves_existing_values_and_hot_reloads(
    tmp_path: Path,
) -> None:
    accounts_path = tmp_path / "accounts.json"
    settings = make_settings(
        accounts_file_path=str(accounts_path),
        accounts_reload_interval_seconds=0,
        config_write_enabled=True,
    )
    accounts_path.write_bytes(serialize_accounts(settings.accounts))
    new_key = "sentinel-new-provider-key-account-a"
    payload = editable_payload(weight=4)
    payload["accounts"][0]["keys"].append({"id": "new-key", "value": new_key})

    async def upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    app = create_app(settings, transport=httpx.MockTransport(upstream))
    async with LifespanManager(app):
        async with await app_client(app) as client:
            saved = await client.put(
                "/admin/v1/config",
                headers=auth_headers(),
                json=payload,
            )
            config = await client.get("/admin/v1/config", headers=auth_headers())

    assert saved.status_code == 200
    assert saved.json()["reload"]["applied"] is True
    assert saved.json()["config"]["accounts"][0]["weight"] == 4
    assert saved.json()["config"]["accounts"][0]["name"] == "主账号"
    assert KEY_A1 not in saved.text
    assert KEY_A2 not in saved.text
    assert OAUTH_A1 not in saved.text
    assert new_key not in saved.text
    stored = json.loads(accounts_path.read_text(encoding="utf-8"))
    account = stored["accounts"][0]
    assert account["name"] == "主账号"
    assert account["weight"] == 4
    assert [item["api_key"] for item in account["keys"]] == [
        KEY_A1,
        KEY_A2,
        new_key,
    ]
    assert account["oauth_tokens"][0]["access_token"] == OAUTH_A1
    assert config.json()["accounts"][0]["weight"] == 4
    assert config.json()["accounts"][0]["name"] == "主账号"
    assert [item["id"] for item in config.json()["accounts"][0]["keys"]] == [
        "primary",
        "standby",
        "new-key",
    ]
    assert list(tmp_path.glob(".accounts-*.tmp")) == []


@pytest.mark.asyncio
async def test_account_delete_is_immediate_persistent_and_cleans_runtime_state(
    tmp_path: Path,
) -> None:
    accounts_path = tmp_path / "accounts.json"
    settings = make_settings(
        multiple_accounts=True,
        routing_mode="round_robin",
        accounts_file_path=str(accounts_path),
        accounts_reload_interval_seconds=0,
        config_write_enabled=True,
    )
    accounts_path.write_bytes(serialize_accounts(settings.accounts))
    app = create_app(settings, transport=httpx.MockTransport(lambda _: None))

    async with LifespanManager(app):
        service = app.state.proxy_service
        await service.state.set_affinities({"delete-account-fixture"}, "account-a", 60)
        await service.state.save_cooldown(
            StoredCooldown(
                scope="account",
                account_id="account-a",
                name="delete-fixture",
                until_epoch=9_999_999_999,
            )
        )
        await service.state.save_quota_snapshot(
            "account-a", 200, {"remaining_credits": 12}
        )

        async with await app_client(app) as client:
            denied = await client.delete("/admin/v1/accounts/account-a")
            deleted = await client.delete(
                "/admin/v1/accounts/account-a", headers=auth_headers()
            )
            config = await client.get("/admin/v1/config", headers=auth_headers())
            status = await client.get("/admin/v1/accounts", headers=auth_headers())
            unknown = await client.delete(
                "/admin/v1/accounts/account-a", headers=auth_headers()
            )
            last = await client.delete(
                "/admin/v1/accounts/account-b", headers=auth_headers()
            )

        assert await service.state.get_affinity("delete-account-fixture") is None
        assert not any(
            item.account_id == "account-a"
            for item in await service.state.load_cooldowns()
        )
        assert "account-a" not in await service.state.quota_snapshots()

    assert denied.status_code == 401
    assert deleted.status_code == 200
    assert deleted.headers["cache-control"] == "no-store"
    assert deleted.json()["deleted"] == "account-a"
    assert deleted.json()["reload"]["applied"] is True
    assert config.json()["routing"]["default_account"] == "account-b"
    assert config.json()["routing"]["configured_default_account"] is None
    assert [item["id"] for item in config.json()["accounts"]] == ["account-b"]
    assert [item["id"] for item in status.json()["accounts"]] == ["account-b"]
    assert unknown.status_code == 404
    assert unknown.json() == {"detail": "account_not_found"}
    assert last.status_code == 409
    assert last.json() == {"detail": "last_account_required"}
    stored = json.loads(accounts_path.read_text(encoding="utf-8"))
    assert [item["id"] for item in stored["accounts"]] == ["account-b"]
    assert list(tmp_path.glob(".accounts-*.tmp")) == []


@pytest.mark.asyncio
async def test_delete_during_stream_does_not_restore_removed_account_affinity(
    tmp_path: Path,
) -> None:
    accounts_path = tmp_path / "accounts.json"
    settings = make_settings(
        multiple_accounts=True,
        routing_mode="round_robin",
        accounts_file_path=str(accounts_path),
        accounts_reload_interval_seconds=0,
        config_write_enabled=True,
    )
    accounts_path.write_bytes(serialize_accounts(settings.accounts))
    stream_started = asyncio.Event()
    release_stream = asyncio.Event()

    async def upstream(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            stream=_BlockingAdminResponseStream(stream_started, release_stream),
        )

    app = create_app(settings, transport=httpx.MockTransport(upstream))
    async with LifespanManager(app):
        service = app.state.proxy_service
        async with (
            await app_client(app) as slow_client,
            await app_client(app) as control,
        ):
            slow_request = asyncio.create_task(
                slow_client.post(
                    "/api/v1/search",
                    headers={
                        **auth_headers(),
                        "X-QVeris-Account": "account-a",
                    },
                    json={"query": "slow"},
                )
            )
            await asyncio.wait_for(stream_started.wait(), timeout=1)
            try:
                deleted = await asyncio.wait_for(
                    control.delete(
                        "/admin/v1/accounts/account-a", headers=auth_headers()
                    ),
                    timeout=1,
                )
            finally:
                release_stream.set()
            streamed = await asyncio.wait_for(slow_request, timeout=1)

        assert await service.state.get_affinity("search_id:deleted-stream") is None

    assert deleted.status_code == 200
    assert streamed.status_code == 200
    assert streamed.json() == {"search_id": "deleted-stream"}


@pytest.mark.asyncio
async def test_account_delete_rolls_back_when_state_cleanup_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    accounts_path = tmp_path / "accounts.json"
    settings = make_settings(
        multiple_accounts=True,
        routing_mode="round_robin",
        accounts_file_path=str(accounts_path),
        accounts_reload_interval_seconds=0,
        config_write_enabled=True,
    )
    accounts_path.write_bytes(serialize_accounts(settings.accounts))
    app = create_app(settings, transport=httpx.MockTransport(lambda _: None))

    async with LifespanManager(app):
        service = app.state.proxy_service

        async def fail_cleanup(_: set[str]) -> None:
            raise RuntimeError("state cleanup fixture")

        monkeypatch.setattr(service.state, "purge_accounts", fail_cleanup)
        async with await app_client(app) as client:
            response = await client.delete(
                "/admin/v1/accounts/account-a", headers=auth_headers()
            )
            config = await client.get("/admin/v1/config", headers=auth_headers())

    assert response.status_code == 409
    assert response.json() == {"detail": "apply_failed"}
    assert [item["id"] for item in config.json()["accounts"]] == [
        "account-a",
        "account-b",
    ]
    stored = json.loads(accounts_path.read_text(encoding="utf-8"))
    assert [item["id"] for item in stored["accounts"]] == [
        "account-a",
        "account-b",
    ]


@pytest.mark.asyncio
async def test_explicit_default_account_must_be_changed_before_delete(
    tmp_path: Path,
) -> None:
    accounts_path = tmp_path / "accounts.json"
    settings = make_settings(
        multiple_accounts=True,
        default_account="account-b",
        accounts_file_path=str(accounts_path),
        accounts_reload_interval_seconds=0,
        config_write_enabled=True,
    )
    accounts_path.write_bytes(serialize_accounts(settings.accounts))
    app = create_app(settings, transport=httpx.MockTransport(lambda _: None))

    async with LifespanManager(app):
        async with await app_client(app) as client:
            config = await client.get("/admin/v1/config", headers=auth_headers())
            response = await client.delete(
                "/admin/v1/accounts/account-b", headers=auth_headers()
            )

    assert config.json()["routing"]["configured_default_account"] == "account-b"
    assert response.status_code == 409
    assert response.json() == {"detail": "default_account_locked"}
    stored = json.loads(accounts_path.read_text(encoding="utf-8"))
    assert [item["id"] for item in stored["accounts"]] == [
        "account-a",
        "account-b",
    ]


@pytest.mark.asyncio
async def test_account_test_uses_only_fixed_low_cost_endpoints() -> None:
    calls: list[tuple[str, str]] = []

    async def upstream(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.path.endswith("/auth/credits"):
            assert request.headers["authorization"] in {
                f"Bearer {KEY_A1}",
                f"Bearer {KEY_A2}",
            }
            return httpx.Response(
                200,
                json={"data": {"remaining_credits": 42}},
            )
        if request.url.path.endswith("/auth/verify-token"):
            assert request.headers["authorization"] == f"Bearer {OAUTH_A1}"
            return httpx.Response(200, json={"valid": True})
        raise AssertionError("unexpected upstream test route")

    app = create_app(make_settings(), transport=httpx.MockTransport(upstream))
    async with LifespanManager(app):
        startup_probe = await app.state.proxy_service.pool.acquire(
            "account-a",
            "auth/credits",
            control=True,
        )
        await startup_probe.release()
        async with await app_client(app) as client:
            response = await client.post(
                "/admin/v1/accounts/account-a/test",
                headers=auth_headers(),
            )
            unknown = await client.post(
                "/admin/v1/accounts/unknown/test",
                headers=auth_headers(),
            )
            status = await client.get(
                "/admin/v1/accounts",
                headers=auth_headers(),
            )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    checks = response.json()["checks"]
    assert {check["credential_kind"] for check in checks} == {"api_key", "oauth"}
    assert next(check for check in checks if check["credential_kind"] == "api_key")[
        "credits"
    ] == {"data.remaining_credits": 42}
    assert calls == [
        ("GET", "/api/v1/auth/credits"),
        ("POST", "/api/v1/auth/verify-token"),
    ]
    assert unknown.status_code == 404
    assert status.json()["accounts"][0]["quota"]["credits"] == {
        "data.remaining_credits": 42
    }
    assert KEY_A1 not in response.text
    assert OAUTH_A1 not in response.text


@pytest.mark.asyncio
async def test_admin_config_payload_limit_is_enforced_before_parsing() -> None:
    app = create_app(make_settings(), transport=httpx.MockTransport(lambda _: None))
    oversized = b"{" + (b"x" * (256 * 1024))
    async with LifespanManager(app):
        async with await app_client(app) as client:
            response = await client.post(
                "/admin/v1/config/validate",
                headers={
                    **auth_headers(),
                    "Content-Type": "application/json",
                },
                content=oversized,
            )

    assert response.status_code == 413
    assert response.json() == {"detail": "configuration is too large"}
