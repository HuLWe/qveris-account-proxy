from __future__ import annotations

import base64
import re
import time
from pathlib import Path

import httpx
import pytest
from asgi_lifespan import LifespanManager

from qveris_proxy.app import create_app
from conftest import ACCESS_TOKEN, make_settings


PROXY_KEY_PATTERN = re.compile(r"^sk-[A-Za-z0-9_-]{43}$")


def admin_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {ACCESS_TOKEN}"}


def proxy_headers(secret: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {secret}",
        "X-QVeris-Account": "account-a",
    }


async def create_managed_key(
    client: httpx.AsyncClient, **overrides: object
) -> tuple[dict[str, object], str]:
    payload: dict[str, object] = {
        "name": "Desktop client",
        "enabled": True,
        "request_limit": None,
        "requests_per_minute": None,
        "max_concurrency": 8,
        "expires_at": None,
    }
    payload.update(overrides)
    response = await client.post(
        "/admin/v1/proxy-keys",
        headers=admin_headers(),
        json=payload,
    )
    assert response.status_code == 200, response.text
    document = response.json()
    return document["key"], document["secret"]


@pytest.mark.asyncio
async def test_proxy_key_admin_crud_returns_secret_only_when_created(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "proxy-keys.db"

    async def upstream(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    app = create_app(
        make_settings(state_path=str(state_path)),
        transport=httpx.MockTransport(upstream),
    )
    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://proxy.test",
        ) as client:
            initial = await client.get("/admin/v1/proxy-keys", headers=admin_headers())
            assert initial.status_code == 200
            assert [key["id"] for key in initial.json()["keys"]] == ["primary"]
            assert "secret" not in initial.text
            assert "secret_hash" not in initial.text

            expires_at = time.time() + 3600
            key, secret = await create_managed_key(
                client,
                name="  Build agent  ",
                request_limit=25,
                requests_per_minute=12,
                max_concurrency=3,
                expires_at=expires_at,
            )
            assert PROXY_KEY_PATTERN.fullmatch(secret)
            assert len(base64.urlsafe_b64decode(secret[3:] + "=")) == 32
            assert key == {
                **key,
                "kind": "managed",
                "name": "Build agent",
                "enabled": True,
                "request_limit": 25,
                "requests_used": 0,
                "requests_per_minute": 12,
                "max_concurrency": 3,
                "expires_at": pytest.approx(expires_at),
                "active_requests": 0,
            }

            listed = await client.get("/admin/v1/proxy-keys", headers=admin_headers())
            assert listed.status_code == 200
            assert secret not in listed.text
            assert "secret_hash" not in listed.text
            assert len(listed.json()["keys"]) == 2

            updated = await client.patch(
                f"/admin/v1/proxy-keys/{key['id']}",
                headers=admin_headers(),
                json={"name": "CI client", "request_limit": None},
            )
            assert updated.status_code == 200
            updated_key = updated.json()["key"]
            assert updated_key["name"] == "CI client"
            assert updated_key["request_limit"] is None
            assert updated_key["max_concurrency"] == 3
            assert secret not in updated.text

            reset = await client.post(
                f"/admin/v1/proxy-keys/{key['id']}/reset-usage",
                headers=admin_headers(),
            )
            assert reset.status_code == 200
            assert reset.json()["key"]["requests_used"] == 0

            empty_update = await client.patch(
                f"/admin/v1/proxy-keys/{key['id']}",
                headers=admin_headers(),
                json={},
            )
            missing = await client.patch(
                "/admin/v1/proxy-keys/not-found",
                headers=admin_headers(),
                json={"enabled": False},
            )
            protected = await client.delete(
                "/admin/v1/proxy-keys/primary", headers=admin_headers()
            )
            assert empty_update.status_code == 400
            assert missing.status_code == 404
            assert protected.status_code == 409

            deleted = await client.delete(
                f"/admin/v1/proxy-keys/{key['id']}", headers=admin_headers()
            )
            assert deleted.status_code == 200
            assert deleted.json() == {"deleted": key["id"]}

    database_bytes = state_path.read_bytes()
    assert secret.encode() not in database_bytes
    assert ACCESS_TOKEN.encode() not in database_bytes


@pytest.mark.asyncio
async def test_managed_proxy_key_limits_reset_and_admin_isolation(
    tmp_path: Path,
) -> None:
    upstream_calls = 0

    async def upstream(_: httpx.Request) -> httpx.Response:
        nonlocal upstream_calls
        upstream_calls += 1
        return httpx.Response(200, json={"ok": True})

    app = create_app(
        make_settings(state_path=str(tmp_path / "limits.db")),
        transport=httpx.MockTransport(upstream),
    )
    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://proxy.test",
        ) as client:
            key, secret = await create_managed_key(client, request_limit=2)

            denied_admin = await client.get(
                "/admin/v1/proxy-keys",
                headers={"Authorization": f"Bearer {secret}"},
            )
            first = await client.post(
                "/api/v1/search",
                headers=proxy_headers(secret),
                json={"query": "one"},
            )
            second = await client.post(
                "/api/v1/search",
                headers=proxy_headers(secret),
                json={"query": "two"},
            )
            exhausted = await client.post(
                "/api/v1/search",
                headers=proxy_headers(secret),
                json={"query": "three"},
            )

            assert denied_admin.status_code == 401
            assert first.status_code == 200
            assert second.status_code == 200
            assert exhausted.status_code == 429
            assert "retry-after" not in exhausted.headers
            assert upstream_calls == 2

            listed = await client.get("/admin/v1/proxy-keys", headers=admin_headers())
            managed = next(
                item for item in listed.json()["keys"] if item["id"] == key["id"]
            )
            assert managed["requests_used"] == 2

            reset = await client.post(
                f"/admin/v1/proxy-keys/{key['id']}/reset-usage",
                headers=admin_headers(),
            )
            resumed = await client.post(
                "/api/v1/search",
                headers=proxy_headers(secret),
                json={"query": "after reset"},
            )
            assert reset.status_code == 200
            assert resumed.status_code == 200

            disabled = await client.patch(
                f"/admin/v1/proxy-keys/{key['id']}",
                headers=admin_headers(),
                json={"enabled": False},
            )
            rejected = await client.post(
                "/api/v1/search",
                headers=proxy_headers(secret),
                json={"query": "disabled"},
            )
            assert disabled.status_code == 200
            assert rejected.status_code == 401
            assert rejected.headers["www-authenticate"] == "Bearer"
            assert upstream_calls == 3


@pytest.mark.asyncio
async def test_proxy_key_rpm_expiry_and_restart_persistence(tmp_path: Path) -> None:
    state_path = str(tmp_path / "persistent-keys.db")
    calls = 0

    async def upstream(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"ok": True})

    settings = make_settings(state_path=state_path)
    first_app = create_app(settings, transport=httpx.MockTransport(upstream))
    async with LifespanManager(first_app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=first_app),
            base_url="http://proxy.test",
        ) as client:
            rpm_key, rpm_secret = await create_managed_key(
                client, name="RPM", requests_per_minute=1
            )
            expiring_key, expiring_secret = await create_managed_key(
                client,
                name="Expiring",
                expires_at=time.time() + 3600,
            )
            first = await client.post(
                "/api/v1/search",
                headers=proxy_headers(rpm_secret),
                json={"query": "allowed"},
            )
            limited = await client.post(
                "/api/v1/search",
                headers=proxy_headers(rpm_secret),
                json={"query": "limited"},
            )
            assert first.status_code == 200
            assert limited.status_code == 429
            assert int(limited.headers["retry-after"]) >= 1

            expiry = float(expiring_key["expires_at"])
            first_app.state.proxy_service.state._wall_time = lambda: expiry + 1
            expired = await client.post(
                "/api/v1/search",
                headers=proxy_headers(expiring_secret),
                json={"query": "expired"},
            )
            assert expired.status_code == 401

    second_app = create_app(settings, transport=httpx.MockTransport(upstream))
    async with LifespanManager(second_app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=second_app),
            base_url="http://proxy.test",
        ) as client:
            listed = await client.get("/admin/v1/proxy-keys", headers=admin_headers())
            keys = listed.json()["keys"]
            assert {item["id"] for item in keys} == {
                "primary",
                rpm_key["id"],
                expiring_key["id"],
            }
            persisted = next(item for item in keys if item["id"] == rpm_key["id"])
            assert persisted["requests_used"] == 1

            still_limited = await client.post(
                "/api/v1/search",
                headers=proxy_headers(rpm_secret),
                json={"query": "after restart"},
            )
            assert still_limited.status_code == 429

            primary = await client.post(
                "/api/v1/search",
                headers=proxy_headers(ACCESS_TOKEN),
                json={"query": "legacy primary"},
            )
            assert primary.status_code == 200

    database_bytes = Path(state_path).read_bytes()
    assert rpm_secret.encode() not in database_bytes
    assert expiring_secret.encode() not in database_bytes
    assert calls == 2


@pytest.mark.asyncio
async def test_many_proxy_keys_are_unique_and_independent(tmp_path: Path) -> None:
    async def upstream(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    app = create_app(
        make_settings(state_path=str(tmp_path / "many.db")),
        transport=httpx.MockTransport(upstream),
    )
    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://proxy.test",
        ) as client:
            created = [
                await create_managed_key(client, name=f"Client {index}")
                for index in range(32)
            ]
            key_ids = {key["id"] for key, _ in created}
            secrets = {secret for _, secret in created}
            assert len(key_ids) == 32
            assert len(secrets) == 32
            assert all(PROXY_KEY_PATTERN.fullmatch(secret) for secret in secrets)

            listed = await client.get("/admin/v1/proxy-keys", headers=admin_headers())
            assert len(listed.json()["keys"]) == 33
            assert all(secret not in listed.text for secret in secrets)
