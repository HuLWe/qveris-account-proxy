from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from asgi_lifespan import LifespanManager

from qveris_proxy.app import create_app
from qveris_proxy.state import StateStore, StoredCooldown
from conftest import (
    ACCESS_TOKEN,
    KEY_A1,
    KEY_A2,
    KEY_B1,
    OAUTH_A1,
    OAUTH_B1,
    make_settings,
)
from test_app import auth_headers


async def app_client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://proxy.test"
    )


@pytest.mark.asyncio
async def test_round_robin_accounts_and_session_affinity(tmp_path: Path) -> None:
    async def upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    settings = make_settings(
        multiple_accounts=True,
        routing_mode="round_robin",
        state_path=str(tmp_path / "state.db"),
    )
    app = create_app(settings, transport=httpx.MockTransport(upstream))
    async with LifespanManager(app):
        async with await app_client(app) as client:
            first = await client.post(
                "/api/v1/search",
                headers={"Authorization": f"Bearer {ACCESS_TOKEN}"},
                json={"query": "weather", "session_id": "session-a"},
            )
            second = await client.post(
                "/api/v1/search",
                headers={"Authorization": f"Bearer {ACCESS_TOKEN}"},
                json={"query": "weather", "session_id": "session-b"},
            )
            sticky = await client.post(
                "/api/v1/tools/by-ids",
                headers={"Authorization": f"Bearer {ACCESS_TOKEN}"},
                json={"tool_ids": ["fixture.tool"], "session_id": "session-a"},
            )

    assert first.headers["x-qveris-proxy-account"] == "account-a"
    assert second.headers["x-qveris-proxy-account"] == "account-b"
    assert sticky.headers["x-qveris-proxy-account"] == "account-a"


@pytest.mark.asyncio
async def test_affinity_survives_container_restart(tmp_path: Path) -> None:
    state_path = str(tmp_path / "state.db")

    async def upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    settings = make_settings(
        multiple_accounts=True,
        routing_mode="round_robin",
        state_path=state_path,
    )

    first_app = create_app(settings, transport=httpx.MockTransport(upstream))
    async with LifespanManager(first_app):
        async with await app_client(first_app) as client:
            await client.post(
                "/api/v1/search",
                headers={"Authorization": f"Bearer {ACCESS_TOKEN}"},
                json={"query": "prelude", "session_id": "prelude-session"},
            )
            persisted = await client.post(
                "/api/v1/search",
                headers={"Authorization": f"Bearer {ACCESS_TOKEN}"},
                json={"query": "target", "session_id": "persistent-session"},
            )
    assert persisted.headers["x-qveris-proxy-account"] == "account-b"

    second_app = create_app(settings, transport=httpx.MockTransport(upstream))
    async with LifespanManager(second_app):
        async with await app_client(second_app) as client:
            restored = await client.post(
                "/api/v1/tools/by-ids",
                headers={"Authorization": f"Bearer {ACCESS_TOKEN}"},
                json={
                    "tool_ids": ["fixture.tool"],
                    "session_id": "persistent-session",
                },
            )

    assert restored.headers["x-qveris-proxy-account"] == "account-b"


@pytest.mark.asyncio
async def test_search_id_from_response_creates_affinity(tmp_path: Path) -> None:
    calls = 0

    async def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if request.url.path.endswith("/search"):
            return httpx.Response(
                200,
                json={"search_id": "srch_persisted", "results": []},
            )
        return httpx.Response(200, json={"tools": []})

    settings = make_settings(
        multiple_accounts=True,
        routing_mode="round_robin",
        state_path=str(tmp_path / "state.db"),
    )
    app = create_app(settings, transport=httpx.MockTransport(upstream))
    async with LifespanManager(app):
        async with await app_client(app) as client:
            search = await client.post(
                "/api/v1/search",
                headers={"Authorization": f"Bearer {ACCESS_TOKEN}"},
                json={"query": "weather"},
            )
            inspect = await client.post(
                "/api/v1/tools/by-ids",
                headers={"Authorization": f"Bearer {ACCESS_TOKEN}"},
                json={
                    "tool_ids": ["fixture.tool"],
                    "search_id": "srch_persisted",
                },
            )

    assert calls == 2
    assert search.headers["x-qveris-proxy-account"] == "account-a"
    assert inspect.headers["x-qveris-proxy-account"] == "account-a"


@pytest.mark.asyncio
async def test_rate_limit_cooldown_survives_restart(tmp_path: Path) -> None:
    state_path = str(tmp_path / "state.db")
    upstream_calls = 0

    async def limited_upstream(request: httpx.Request) -> httpx.Response:
        nonlocal upstream_calls
        upstream_calls += 1
        return httpx.Response(429, headers={"Retry-After": "60"})

    settings = make_settings(state_path=state_path)
    first_app = create_app(settings, transport=httpx.MockTransport(limited_upstream))
    async with LifespanManager(first_app):
        async with await app_client(first_app) as client:
            limited = await client.post(
                "/api/v1/tools/execute",
                headers=auth_headers(),
                json={"tool_id": "fixture.tool", "parameters": {}},
            )
    assert limited.status_code == 429
    assert upstream_calls == 1

    async def should_not_run(request: httpx.Request) -> httpx.Response:
        raise AssertionError("persisted cooldown should stop the upstream request")

    second_app = create_app(settings, transport=httpx.MockTransport(should_not_run))
    async with LifespanManager(second_app):
        async with await app_client(second_app) as client:
            restored = await client.post(
                "/api/v1/tools/execute",
                headers=auth_headers(),
                json={"tool_id": "fixture.tool", "parameters": {}},
            )

    assert restored.status_code == 429
    assert 1 <= int(restored.headers["retry-after"]) <= 60


@pytest.mark.asyncio
async def test_background_quota_refresh_and_admin_status_are_secret_free(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "state.db"
    refreshed = asyncio.Event()
    quota_calls = 0

    async def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal quota_calls
        if request.url.path.endswith("/auth/credits"):
            quota_calls += 1
            auth = request.headers["authorization"]
            remaining = 900 if KEY_B1 in auth else 500
            if quota_calls >= 2:
                refreshed.set()
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {"total_available_credits": remaining},
                },
            )
        return httpx.Response(200, json={"ok": True})

    settings = make_settings(
        multiple_accounts=True,
        routing_mode="round_robin",
        state_path=str(state_path),
        quota_refresh_interval_seconds=3600,
    )
    app = create_app(settings, transport=httpx.MockTransport(upstream))
    async with LifespanManager(app):
        await asyncio.wait_for(refreshed.wait(), timeout=2)
        async with await app_client(app) as client:
            status = await client.get(
                "/admin/v1/accounts",
                headers={"Authorization": f"Bearer {ACCESS_TOKEN}"},
            )

    assert status.status_code == 200
    accounts = {item["id"]: item for item in status.json()["accounts"]}
    assert accounts["account-a"]["quota"]["credits"] == {
        "data.total_available_credits": 500
    }
    assert accounts["account-b"]["quota"]["credits"] == {
        "data.total_available_credits": 900
    }
    rendered = status.text
    assert ACCESS_TOKEN not in rendered
    assert KEY_A1 not in rendered
    assert KEY_A2 not in rendered
    assert KEY_B1 not in rendered
    assert OAUTH_A1 not in rendered
    assert OAUTH_B1 not in rendered

    database_bytes = state_path.read_bytes()
    assert ACCESS_TOKEN.encode() not in database_bytes
    assert KEY_A1.encode() not in database_bytes
    assert KEY_A2.encode() not in database_bytes
    assert KEY_B1.encode() not in database_bytes
    assert OAUTH_A1.encode() not in database_bytes
    assert OAUTH_B1.encode() not in database_bytes


@pytest.mark.asyncio
async def test_concurrent_quota_refreshes_are_serialized(tmp_path: Path) -> None:
    active = 0
    max_active = 0
    calls = 0

    async def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal active, calls, max_active
        assert request.url.path.endswith("/auth/credits")
        calls += 1
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.02)
        active -= 1
        return httpx.Response(200, json={"total_available_credits": 100})

    settings = make_settings(state_path=str(tmp_path / "state.db"))
    app = create_app(settings, transport=httpx.MockTransport(upstream))
    async with LifespanManager(app):
        service = app.state.proxy_service
        await asyncio.gather(service.refresh_quotas(), service.refresh_quotas())

    assert calls == 2
    assert max_active == 1


@pytest.mark.asyncio
async def test_slow_quota_probe_does_not_freeze_data_plane_during_reload(
    tmp_path: Path,
) -> None:
    accounts_path = tmp_path / "accounts.json"

    def write_accounts(include_third: bool) -> None:
        accounts = [
            {
                "id": "account-a",
                "requests_per_minute": 10_000,
                "burst": 10_000,
                "keys": [{"id": "primary", "api_key": KEY_A1}],
            },
            {
                "id": "account-b",
                "requests_per_minute": 10_000,
                "burst": 10_000,
                "keys": [{"id": "primary", "api_key": KEY_B1}],
            },
        ]
        if include_third:
            accounts.append(
                {
                    "id": "account-c",
                    "requests_per_minute": 10_000,
                    "burst": 10_000,
                    "keys": [
                        {
                            "id": "primary",
                            "api_key": "sentinel-provider-key-account-c-primary",
                        }
                    ],
                }
            )
        accounts_path.write_text(
            json.dumps({"accounts": accounts}),
            encoding="utf-8",
        )

    write_accounts(False)
    quota_started = asyncio.Event()
    release_quota = asyncio.Event()

    async def upstream(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/credits"):
            if not quota_started.is_set():
                quota_started.set()
                await release_quota.wait()
            return httpx.Response(200, json={"remaining_credits": 100})
        return httpx.Response(200, json={"ok": True})

    settings = make_settings(
        multiple_accounts=True,
        accounts_file_path=str(accounts_path),
        accounts_reload_interval_seconds=0,
        state_path=str(tmp_path / "slow-quota.db"),
    )
    app = create_app(settings, transport=httpx.MockTransport(upstream))
    async with LifespanManager(app):
        async with await app_client(app) as client:
            quota_task = asyncio.create_task(app.state.proxy_service.refresh_quotas())
            reload_task: asyncio.Task[httpx.Response] | None = None
            try:
                await asyncio.wait_for(quota_started.wait(), timeout=1)
                write_accounts(True)
                reload_task = asyncio.create_task(
                    client.post(
                        "/admin/v1/reload-accounts",
                        headers=auth_headers(account=None),
                    )
                )
                for _ in range(100):
                    gate_status = await app.state.proxy_service._gate.status()
                    if gate_status["waiting_updates"]:
                        break
                    await asyncio.sleep(0.01)
                assert gate_status["waiting_updates"] == 1

                data_response = await asyncio.wait_for(
                    client.post(
                        "/api/v1/search",
                        headers=auth_headers("account-b"),
                        json={"query": "still-flowing"},
                    ),
                    timeout=1,
                )
                assert data_response.status_code == 200
                assert not reload_task.done()
            finally:
                release_quota.set()

            assert reload_task is not None
            reloaded = await asyncio.wait_for(reload_task, timeout=2)
            await asyncio.wait_for(quota_task, timeout=2)

    assert reloaded.status_code == 200


@pytest.mark.asyncio
async def test_402_persists_until_positive_quota_probe(tmp_path: Path) -> None:
    state_path = str(tmp_path / "state.db")

    async def depleted(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/tools/execute")
        return httpx.Response(402, json={"detail": "fixture depleted"})

    settings = make_settings(
        state_path=state_path,
        payment_required_cooldown_seconds=3600,
    )
    first_app = create_app(settings, transport=httpx.MockTransport(depleted))
    async with LifespanManager(first_app):
        async with await app_client(first_app) as client:
            first = await client.post(
                "/api/v1/tools/execute",
                headers=auth_headers(),
                json={"tool_id": "fixture.tool", "parameters": {}},
            )
    assert first.status_code == 402

    calls: list[str] = []

    async def recovered(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/auth/credits"):
            return httpx.Response(200, json={"total_available_credits": 100})
        return httpx.Response(200, json={"ok": True})

    second_app = create_app(settings, transport=httpx.MockTransport(recovered))
    async with LifespanManager(second_app):
        async with await app_client(second_app) as client:
            blocked = await client.post(
                "/api/v1/tools/execute",
                headers=auth_headers(),
                json={"tool_id": "fixture.tool", "parameters": {}},
            )
            refreshed = await client.post(
                "/admin/v1/refresh-credits",
                headers=auth_headers(),
            )
            successful = await client.post(
                "/api/v1/tools/execute",
                headers=auth_headers(),
                json={"tool_id": "fixture.tool", "parameters": {}},
            )

    assert blocked.status_code == 402
    assert refreshed.status_code == 200
    assert successful.status_code == 200
    assert calls == ["/api/v1/auth/credits", "/api/v1/tools/execute"]


@pytest.mark.asyncio
async def test_retained_health_state_survives_expiry_and_deletes_atomically(
    tmp_path: Path,
) -> None:
    now = [1_700_000_000.0]
    store = StateStore(
        str(tmp_path / "health.db"),
        wall_time=lambda: now[0],
    )
    failure = StoredCooldown(
        scope="health",
        account_id="account-a",
        name="upstream",
        until_epoch=now[0] + 2,
        failure_count=3,
        retain_after_expiry=True,
    )
    await store.save_cooldown(failure)

    now[0] += 3
    restored = await store.load_cooldowns()
    assert restored == [failure]

    await store.save_cooldown(
        StoredCooldown(
            scope="health",
            account_id="account-a",
            name="depleted",
            until_epoch=0,
            retain_after_expiry=True,
        )
    )
    await store.save_cooldown(
        StoredCooldown(
            scope="health",
            account_id="account-a",
            name="upstream",
            until_epoch=0,
            delete=True,
            clears=(("health", "depleted"),),
        )
    )
    assert await store.load_cooldowns() == []
    await store.close()


@pytest.mark.asyncio
async def test_failed_quota_attempt_preserves_last_successful_snapshot(
    tmp_path: Path,
) -> None:
    now = [1_700_000_000.0]
    store = StateStore(
        str(tmp_path / "quota.db"),
        wall_time=lambda: now[0],
    )
    await store.save_quota_snapshot(
        "account-a",
        200,
        {"data.remaining_credits": 125},
    )

    now[0] += 30
    await store.save_quota_snapshot("account-a", 0, {})
    snapshot = (await store.quota_snapshots())["account-a"]

    assert snapshot == {
        "http_status": 0,
        "checked_at": now[0],
        "last_success_at": now[0] - 30,
        "stale": True,
        "credits": {"data.remaining_credits": 125},
    }
    await store.close()


@pytest.mark.asyncio
async def test_403_account_circuit_survives_application_restart(tmp_path: Path) -> None:
    state_path = str(tmp_path / "forbidden.db")
    calls = 0

    async def forbidden(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(403)

    settings = make_settings(
        state_path=state_path,
        forbidden_cooldown_seconds=3600,
    )
    first_app = create_app(settings, transport=httpx.MockTransport(forbidden))
    async with LifespanManager(first_app):
        async with await app_client(first_app) as client:
            response = await client.post(
                "/api/v1/search",
                headers=auth_headers(),
                json={"query": "fixture"},
            )
    assert response.status_code == 403
    assert calls == 1

    async def should_not_run(request: httpx.Request) -> httpx.Response:
        raise AssertionError("persisted account circuit should block upstream")

    second_app = create_app(settings, transport=httpx.MockTransport(should_not_run))
    async with LifespanManager(second_app):
        async with await app_client(second_app) as client:
            blocked = await client.post(
                "/api/v1/tools/execute",
                headers=auth_headers(),
                json={"tool_id": "fixture.tool", "parameters": {}},
            )
    assert blocked.status_code == 503
    assert int(blocked.headers["retry-after"]) > 3500


@pytest.mark.asyncio
async def test_transport_failure_backoff_survives_application_restart(
    tmp_path: Path,
) -> None:
    state_path = str(tmp_path / "transport-failure.db")
    settings = make_settings(
        state_path=state_path,
        failure_backoff_base_seconds=60,
        failure_backoff_max_seconds=60,
    )

    async def unavailable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("fixture unavailable", request=request)

    first_app = create_app(settings, transport=httpx.MockTransport(unavailable))
    async with LifespanManager(first_app):
        async with await app_client(first_app) as client:
            failed = await client.post(
                "/api/v1/search",
                headers=auth_headers(),
                json={"query": "fixture"},
            )
    assert failed.status_code == 502

    async def should_not_run(request: httpx.Request) -> httpx.Response:
        raise AssertionError("persisted upstream backoff should block request")

    second_app = create_app(settings, transport=httpx.MockTransport(should_not_run))
    async with LifespanManager(second_app):
        async with await app_client(second_app) as client:
            blocked = await client.post(
                "/api/v1/search",
                headers=auth_headers(),
                json={"query": "fixture"},
            )
    assert blocked.status_code == 503
    assert int(blocked.headers["retry-after"]) >= 44


@pytest.mark.asyncio
async def test_zero_quota_probe_proactively_depletes_account(tmp_path: Path) -> None:
    calls: list[str] = []

    async def upstream(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/auth/credits"):
            return httpx.Response(200, json={"data": {"remaining_credits": 0}})
        raise AssertionError("depleted account should not execute a tool")

    settings = make_settings(state_path=str(tmp_path / "depleted.db"))
    app = create_app(settings, transport=httpx.MockTransport(upstream))
    async with LifespanManager(app):
        async with await app_client(app) as client:
            refreshed = await client.post(
                "/admin/v1/refresh-credits",
                headers=auth_headers(),
            )
            blocked = await client.post(
                "/api/v1/tools/execute",
                headers=auth_headers(),
                json={"tool_id": "fixture.tool", "parameters": {}},
            )

    assert refreshed.status_code == 200
    assert blocked.status_code == 402
    assert calls == ["/api/v1/auth/credits"]


@pytest.mark.asyncio
async def test_quota_depletion_and_recovery_survive_restarts(tmp_path: Path) -> None:
    state_path = str(tmp_path / "quota-restart.db")
    settings = make_settings(state_path=state_path)

    async def zero_balance(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/credits"):
            return httpx.Response(200, json={"data": {"remaining_credits": 0}})
        raise AssertionError("depleted account should not reach a tool")

    first_app = create_app(settings, transport=httpx.MockTransport(zero_balance))
    async with LifespanManager(first_app):
        async with await app_client(first_app) as client:
            refreshed = await client.post(
                "/admin/v1/refresh-credits",
                headers=auth_headers(),
            )
    assert refreshed.status_code == 200

    calls: list[str] = []

    async def recovered(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/auth/credits"):
            return httpx.Response(200, json={"data": {"remaining_credits": 25}})
        return httpx.Response(200, json={"ok": True})

    second_app = create_app(settings, transport=httpx.MockTransport(recovered))
    async with LifespanManager(second_app):
        async with await app_client(second_app) as client:
            blocked = await client.post(
                "/api/v1/tools/execute",
                headers=auth_headers(),
                json={"tool_id": "fixture.tool", "parameters": {}},
            )
            refreshed = await client.post(
                "/admin/v1/refresh-credits",
                headers=auth_headers(),
            )
            resumed = await client.post(
                "/api/v1/tools/execute",
                headers=auth_headers(),
                json={"tool_id": "fixture.tool", "parameters": {}},
            )

    assert blocked.status_code == 402
    assert refreshed.status_code == 200
    assert resumed.status_code == 200
    assert calls == ["/api/v1/auth/credits", "/api/v1/tools/execute"]

    third_app = create_app(settings, transport=httpx.MockTransport(recovered))
    async with LifespanManager(third_app):
        async with await app_client(third_app) as client:
            persisted_recovery = await client.post(
                "/api/v1/tools/execute",
                headers=auth_headers(),
                json={"tool_id": "fixture.tool", "parameters": {}},
            )

    assert persisted_recovery.status_code == 200
