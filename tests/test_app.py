from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from asgi_lifespan import LifespanManager
from starlette.requests import ClientDisconnect

from qveris_proxy.app import create_app
from qveris_proxy.config import (
    AccountConfig,
    APIKeyConfig,
    HTTPTransportConfig,
    ProxySettings,
)
from qveris_proxy.routes import (
    PUBLIC_OPERATIONS,
    QVERIS_API_VERSION,
    resolve_operation,
)
from conftest import ACCESS_TOKEN, KEY_A1, KEY_A2, KEY_B1, OAUTH_A1, make_settings
from test_routes import materialize_path


def auth_headers(account: str | None = "account-a") -> dict[str, str]:
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}"}
    if account is not None:
        headers["X-QVeris-Account"] = account
    return headers


async def request_app(app, method: str, path: str, **kwargs) -> httpx.Response:
    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://proxy.test"
        ) as client:
            return await client.request(method, path, **kwargs)


@pytest.mark.asyncio
async def test_liveness_never_calls_upstream() -> None:
    calls = 0

    async def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    app = create_app(make_settings(), transport=httpx.MockTransport(upstream))
    response = await request_app(app, "GET", "/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert calls == 0


@pytest.mark.asyncio
async def test_proxy_requires_its_own_access_token() -> None:
    calls = 0

    async def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200)

    app = create_app(make_settings(), transport=httpx.MockTransport(upstream))
    response = await request_app(
        app,
        "POST",
        "/api/v1/search",
        json={"query": "weather"},
    )

    assert response.status_code == 401
    assert response.headers["x-qveris-api-version"] == QVERIS_API_VERSION
    assert calls == 0


@pytest.mark.asyncio
async def test_forwards_to_fixed_origin_and_replaces_authorization() -> None:
    captured: list[httpx.Request] = []

    async def upstream(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            content=b'{"ok":true}',
            headers={
                "Content-Type": "application/json",
                "X-RateLimit-Remaining": "9",
                "Set-Cookie": "drop=this",
            },
        )

    app = create_app(make_settings(), transport=httpx.MockTransport(upstream))
    response = await request_app(
        app,
        "POST",
        "/api/v1/search?lang=en",
        headers={**auth_headers(), "Cookie": "client=secret"},
        json={"query": "weather"},
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert response.headers["x-qveris-proxy-account"] == "account-a"
    assert response.headers["x-ratelimit-remaining"] == "9"
    assert response.headers["x-qveris-api-version"] == QVERIS_API_VERSION
    assert "set-cookie" not in response.headers
    assert len(captured) == 1
    request = captured[0]
    assert str(request.url) == "https://qveris.ai/api/v1/search?lang=en"
    assert request.headers["authorization"] == f"Bearer {KEY_A1}"
    assert "cookie" not in request.headers
    assert json.loads(request.content) == {"query": "weather"}


@pytest.mark.asyncio
async def test_explicit_routing_requires_account_header() -> None:
    calls = 0

    async def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200)

    app = create_app(
        make_settings(multiple_accounts=True),
        transport=httpx.MockTransport(upstream),
    )
    response = await request_app(
        app,
        "POST",
        "/api/v1/search",
        headers=auth_headers(account=None),
        json={"query": "weather"},
    )

    assert response.status_code == 400
    assert calls == 0


@pytest.mark.asyncio
async def test_explicit_routing_uses_configured_default_account() -> None:
    captured: list[str] = []

    async def upstream(request: httpx.Request) -> httpx.Response:
        captured.append(request.headers["authorization"])
        return httpx.Response(200, json={"ok": True})

    app = create_app(
        make_settings(multiple_accounts=True, default_account="account-b"),
        transport=httpx.MockTransport(upstream),
    )
    response = await request_app(
        app,
        "POST",
        "/api/v1/search",
        headers=auth_headers(account=None),
        json={"query": "weather"},
    )

    assert response.status_code == 200
    assert response.headers["x-qveris-proxy-account"] == "account-b"
    assert captured == [f"Bearer {KEY_B1}"]


@pytest.mark.asyncio
async def test_vibe_style_requests_keep_control_default_without_pinning_tools() -> None:
    captured: list[tuple[str, str]] = []

    async def upstream(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v1/")
        captured.append((path, request.headers.get("authorization", "")))
        if path == "auth/credits":
            remaining = (
                77 if request.headers["authorization"] == f"Bearer {KEY_B1}" else 123
            )
            return httpx.Response(
                200,
                json={"data": {"remaining_credits": remaining}},
            )
        if path == "auth/usage/history/v2":
            return httpx.Response(200, json={"data": {"events": []}})
        if path == "search":
            return httpx.Response(200, json={"search_id": "search-fixture"})
        if path == "tools/execute":
            return httpx.Response(200, json={"ok": True})
        raise AssertionError(f"unexpected upstream path: {path}")

    settings = make_settings(
        multiple_accounts=True,
        routing_mode="round_robin",
        default_account="account-a",
    )
    app = create_app(settings, transport=httpx.MockTransport(upstream))
    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://proxy.test"
        ) as client:
            credits = await client.get(
                "/api/v1/auth/credits",
                headers=auth_headers(account=None),
            )
            usage = await client.get(
                "/api/v1/auth/usage/history/v2",
                headers=auth_headers(account=None),
            )
            first_search = await client.post(
                "/api/v1/search",
                headers=auth_headers(account=None),
                json={"query": "status"},
            )
            second_search = await client.post(
                "/api/v1/search",
                headers=auth_headers(account=None),
                json={"query": "status"},
            )
            explicit = await client.post(
                "/api/v1/tools/execute",
                headers=auth_headers(account="account-b"),
                json={"tool_id": "fixture", "parameters": {}},
            )

    assert credits.status_code == 200
    assert credits.json() == {
        "status": "success",
        "data": {
            "remaining_credits": 200,
            "total_available_credits": 200,
        },
        "proxy_pool": {
            "configured_accounts": 2,
            "included_accounts": 2,
            "complete": True,
        },
    }
    assert usage.status_code == 200
    assert first_search.status_code == 200
    assert second_search.status_code == 200
    assert explicit.status_code == 200
    assert first_search.headers["x-qveris-proxy-account"] == "account-a"
    assert second_search.headers["x-qveris-proxy-account"] == "account-b"
    assert explicit.headers["x-qveris-proxy-account"] == "account-b"
    assert captured[0] == ("auth/credits", f"Bearer {KEY_A1}")
    assert captured[1] == ("auth/credits", f"Bearer {KEY_B1}")
    assert captured[2] == ("auth/usage/history/v2", f"Bearer {OAUTH_A1}")
    assert captured[3][0] == "search"
    assert captured[4][0] == "search"
    assert captured[5] == ("tools/execute", f"Bearer {KEY_B1}")


@pytest.mark.asyncio
async def test_credit_summary_keeps_official_fields_and_consumes_proxy_key_once() -> (
    None
):
    captured: list[str] = []

    async def upstream(request: httpx.Request) -> httpx.Response:
        authorization = request.headers["authorization"]
        captured.append(authorization)
        if authorization == f"Bearer {KEY_B1}":
            return httpx.Response(
                200,
                json={"data": {"total_available_credits": 8}},
            )
        return httpx.Response(
            200,
            json={"data": {"remaining_credits": "12.5"}},
        )

    app = create_app(
        make_settings(multiple_accounts=True, routing_mode="round_robin"),
        transport=httpx.MockTransport(upstream),
    )
    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://proxy.test"
        ) as client:
            response = await client.get(
                "/api/v1/auth/credits",
                headers=auth_headers(account=None),
            )
        proxy_key = (await app.state.proxy_service.proxy_access_keys.list())[0]

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-qveris-api-version"] == QVERIS_API_VERSION
    assert "x-qveris-proxy-account" not in response.headers
    assert response.json() == {
        "status": "success",
        "data": {
            "remaining_credits": 20.5,
            "total_available_credits": 20.5,
        },
        "proxy_pool": {
            "configured_accounts": 2,
            "included_accounts": 2,
            "complete": True,
        },
    }
    assert "account-a" not in response.text
    assert "account-b" not in response.text
    assert KEY_A1 not in response.text
    assert KEY_B1 not in response.text
    assert captured == [f"Bearer {KEY_A1}", f"Bearer {KEY_B1}"]
    assert proxy_key.requests_used == 1
    assert proxy_key.active_requests == 0


@pytest.mark.asyncio
async def test_credit_summary_uses_available_accounts_and_keeps_explicit_routing() -> (
    None
):
    captured: list[str] = []

    async def upstream(request: httpx.Request) -> httpx.Response:
        authorization = request.headers["authorization"]
        captured.append(authorization)
        if authorization == f"Bearer {KEY_B1}":
            return httpx.Response(
                200,
                json={"data": {"remaining_credits": 8}, "provider": "fixture"},
            )
        return httpx.Response(503, json={"detail": "temporarily unavailable"})

    app = create_app(
        make_settings(multiple_accounts=True, routing_mode="round_robin"),
        transport=httpx.MockTransport(upstream),
    )
    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://proxy.test"
        ) as client:
            summary = await client.get(
                "/api/v1/auth/credits",
                headers=auth_headers(account=None),
            )
            explicit = await client.get(
                "/api/v1/auth/credits",
                headers=auth_headers(account="account-b"),
            )

    assert summary.status_code == 200
    assert summary.json()["data"] == {
        "remaining_credits": 8,
        "total_available_credits": 8,
    }
    assert summary.json()["proxy_pool"] == {
        "configured_accounts": 2,
        "included_accounts": 1,
        "complete": False,
    }
    assert explicit.status_code == 200
    assert explicit.headers["x-qveris-proxy-account"] == "account-b"
    assert explicit.json() == {
        "data": {"remaining_credits": 8},
        "provider": "fixture",
    }
    assert captured == [
        f"Bearer {KEY_A1}",
        f"Bearer {KEY_B1}",
        f"Bearer {KEY_B1}",
    ]


@pytest.mark.asyncio
async def test_credit_summary_returns_503_when_no_balance_is_available() -> None:
    async def upstream(request: httpx.Request) -> httpx.Response:
        if request.headers["authorization"] == f"Bearer {KEY_B1}":
            return httpx.Response(200, json={"data": {"plan": "fixture"}})
        return httpx.Response(503)

    app = create_app(
        make_settings(multiple_accounts=True, routing_mode="round_robin"),
        transport=httpx.MockTransport(upstream),
    )
    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://proxy.test"
        ) as client:
            response = await client.get(
                "/api/v1/auth/credits",
                headers=auth_headers(account=None),
            )
        proxy_key = (await app.state.proxy_service.proxy_access_keys.list())[0]

    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["retry-after"] == "1"
    assert response.json() == {"detail": "QVeris credit balances are unavailable"}
    assert proxy_key.requests_used == 1
    assert proxy_key.active_requests == 0


@pytest.mark.asyncio
async def test_credit_summary_preserves_explicit_mode_routing() -> None:
    calls = 0

    async def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"data": {"remaining_credits": 10}})

    app = create_app(
        make_settings(multiple_accounts=True, routing_mode="explicit"),
        transport=httpx.MockTransport(upstream),
    )
    response = await request_app(
        app,
        "GET",
        "/api/v1/auth/credits",
        headers=auth_headers(account=None),
    )

    assert response.status_code == 400
    assert response.json() == {
        "detail": "X-QVeris-Account is required for this request"
    }
    assert calls == 0


@pytest.mark.asyncio
async def test_429_cools_account_and_is_not_retried() -> None:
    calls = 0

    async def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if request.url.path.endswith("/tools/execute"):
            return httpx.Response(429, headers={"Retry-After": "30"})
        return httpx.Response(200, json={"ok": True})

    app = create_app(make_settings(), transport=httpx.MockTransport(upstream))
    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://proxy.test"
        ) as client:
            first = await client.post(
                "/api/v1/tools/execute",
                headers=auth_headers(),
                json={"tool_id": "example.tool", "parameters": {}},
            )
            second = await client.post(
                "/api/v1/tools/execute",
                headers=auth_headers(),
                json={"tool_id": "example.tool", "parameters": {}},
            )
            search = await client.post(
                "/api/v1/search",
                headers=auth_headers(),
                json={"query": "weather"},
            )

    assert first.status_code == 429
    assert second.status_code == 429
    assert second.headers["retry-after"] == "30"
    assert search.status_code == 200
    assert calls == 2


@pytest.mark.asyncio
async def test_public_meta_needs_neither_proxy_token_nor_provider_key() -> None:
    captured: list[httpx.Request] = []

    async def upstream(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"version": QVERIS_API_VERSION})

    app = create_app(
        make_settings(multiple_accounts=True),
        transport=httpx.MockTransport(upstream),
    )
    response = await request_app(app, "GET", "/api/v1/meta")

    assert response.status_code == 200
    assert response.json() == {"version": QVERIS_API_VERSION}
    assert response.headers["x-qveris-api-version"] == QVERIS_API_VERSION
    assert len(captured) == 1
    assert str(captured[0].url) == "https://qveris.ai/api/v1/meta"
    assert "authorization" not in captured[0].headers
    assert "x-qveris-proxy-account" not in response.headers


@pytest.mark.asyncio
async def test_oauth_route_never_falls_back_to_an_api_key() -> None:
    calls = 0

    async def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200)

    settings = ProxySettings(
        accounts=(
            AccountConfig(
                id="api-only",
                keys=(APIKeyConfig(id="primary", api_key=KEY_A1),),
            ),
        ),
        proxy_access_token=ACCESS_TOKEN,
        state_path=":memory:",
        quota_refresh_interval_seconds=0,
    )
    app = create_app(settings, transport=httpx.MockTransport(upstream))
    response = await request_app(
        app,
        "GET",
        "/api/v1/auth/credits/ledger",
        headers=auth_headers(account="api-only"),
    )

    assert response.status_code == 503
    assert calls == 0


@pytest.mark.asyncio
async def test_oauth_route_can_fall_back_to_api_key_for_vibe_compatibility() -> None:
    captured: list[str] = []

    async def upstream(request: httpx.Request) -> httpx.Response:
        captured.append(request.headers["authorization"])
        return httpx.Response(200, json={"data": {"events": []}})

    settings = ProxySettings(
        accounts=(
            AccountConfig(
                id="api-only",
                keys=(APIKeyConfig(id="primary", api_key=KEY_A1),),
            ),
        ),
        proxy_access_token=ACCESS_TOKEN,
        state_path=":memory:",
        quota_refresh_interval_seconds=0,
        allow_oauth_route_fallback=True,
    )
    app = create_app(settings, transport=httpx.MockTransport(upstream))
    response = await request_app(
        app,
        "GET",
        "/api/v1/auth/usage/history/v2",
        headers=auth_headers(account="api-only"),
    )

    assert response.status_code == 200
    assert captured == [f"Bearer {KEY_A1}"]


@pytest.mark.asyncio
@pytest.mark.parametrize("fallback_status", [401, 402, 403, 500, 503])
async def test_rejected_oauth_fallback_does_not_cool_search_key(
    fallback_status: int,
) -> None:
    captured: list[tuple[str, str]] = []

    async def upstream(request: httpx.Request) -> httpx.Response:
        captured.append((request.url.path, request.headers["authorization"]))
        if request.url.path.endswith("/auth/usage/history/v2"):
            return httpx.Response(fallback_status)
        return httpx.Response(200, json={"data": {"results": []}})

    settings = ProxySettings(
        accounts=(
            AccountConfig(
                id="api-only",
                keys=(APIKeyConfig(id="primary", api_key=KEY_A1),),
            ),
        ),
        proxy_access_token=ACCESS_TOKEN,
        state_path=":memory:",
        quota_refresh_interval_seconds=0,
        allow_oauth_route_fallback=True,
    )
    app = create_app(settings, transport=httpx.MockTransport(upstream))
    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://proxy.test"
        ) as client:
            usage = await client.get(
                "/api/v1/auth/usage/history/v2",
                headers=auth_headers(account="api-only"),
            )
            search = await client.post(
                "/api/v1/search",
                headers=auth_headers(account="api-only"),
                json={"query": "fixture"},
            )

    assert usage.status_code == fallback_status
    assert search.status_code == 200
    assert captured == [
        (
            "/api/v1/auth/usage/history/v2",
            f"Bearer {KEY_A1}",
        ),
        ("/api/v1/search", f"Bearer {KEY_A1}"),
    ]


@pytest.mark.asyncio
async def test_oauth_fallback_is_limited_to_vibe_usage_history() -> None:
    calls = 0

    async def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200)

    settings = ProxySettings(
        accounts=(
            AccountConfig(
                id="api-only",
                keys=(APIKeyConfig(id="primary", api_key=KEY_A1),),
            ),
        ),
        proxy_access_token=ACCESS_TOKEN,
        state_path=":memory:",
        quota_refresh_interval_seconds=0,
        allow_oauth_route_fallback=True,
    )
    app = create_app(settings, transport=httpx.MockTransport(upstream))
    response = await request_app(
        app,
        "GET",
        "/api/v1/auth/credits/ledger",
        headers=auth_headers(account="api-only"),
    )

    assert response.status_code == 503
    assert calls == 0


@pytest.mark.asyncio
async def test_oauth_fallback_429_only_cools_usage_route() -> None:
    captured: list[str] = []

    async def upstream(request: httpx.Request) -> httpx.Response:
        captured.append(request.url.path)
        if request.url.path.endswith("/auth/usage/history/v2"):
            return httpx.Response(429, headers={"Retry-After": "30"})
        return httpx.Response(200, json={"data": {"results": []}})

    settings = ProxySettings(
        accounts=(
            AccountConfig(
                id="api-only",
                requests_per_minute=60,
                keys=(APIKeyConfig(id="primary", api_key=KEY_A1),),
            ),
        ),
        proxy_access_token=ACCESS_TOKEN,
        state_path=":memory:",
        quota_refresh_interval_seconds=0,
        allow_oauth_route_fallback=True,
    )
    app = create_app(settings, transport=httpx.MockTransport(upstream))
    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://proxy.test"
        ) as client:
            first_usage = await client.get(
                "/api/v1/auth/usage/history/v2",
                headers=auth_headers(account="api-only"),
            )
            search = await client.post(
                "/api/v1/search",
                headers=auth_headers(account="api-only"),
                json={"query": "fixture"},
            )
            second_usage = await client.get(
                "/api/v1/auth/usage/history/v2",
                headers=auth_headers(account="api-only"),
            )

    assert first_usage.status_code == 429
    assert search.status_code == 200
    assert second_usage.status_code == 429
    assert captured == [
        "/api/v1/auth/usage/history/v2",
        "/api/v1/search",
    ]


@pytest.mark.asyncio
async def test_all_19_public_operations_are_forwarded() -> None:
    captured: list[tuple[str, str]] = []

    async def upstream(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v1/")
        captured.append((request.method, path))
        operation = resolve_operation(request.method, path)
        assert operation is not None
        if operation.credential_kind is None:
            assert "authorization" not in request.headers
        elif operation.credential_kind == "oauth":
            assert request.headers["authorization"] == f"Bearer {OAUTH_A1}"
        else:
            assert request.headers["authorization"].startswith(
                "Bearer sentinel-provider-key-"
            )
        return httpx.Response(200, json={"ok": True})

    app = create_app(make_settings(), transport=httpx.MockTransport(upstream))
    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://proxy.test"
        ) as client:
            for method, path_template in PUBLIC_OPERATIONS:
                path = materialize_path(path_template)
                kwargs = {}
                if path != "meta":
                    kwargs["headers"] = auth_headers()
                if method == "POST" and path != "auth/verify-token":
                    kwargs["json"] = {}
                response = await client.request(method, f"/api/v1/{path}", **kwargs)
                assert response.status_code == 200, (method, path, response.text)

    expected = {
        (method, materialize_path(path_template))
        for method, path_template in PUBLIC_OPERATIONS
    }
    assert set(captured) == expected
    assert len(captured) == 19


@pytest.mark.asyncio
async def test_discover_inspect_call_flow_can_invoke_any_returned_tool() -> None:
    tool_id = "provider.weather.current.v1"
    search_id = "srch_fixture_001"
    session_id = "session-fixture-001"
    steps: list[str] = []

    async def upstream(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"].startswith(
            "Bearer sentinel-provider-key-"
        )
        payload = json.loads(request.content)
        if request.url.path.endswith("/search"):
            steps.append("discover")
            assert payload == {
                "query": "current weather capability",
                "limit": 5,
                "session_id": session_id,
                "view": "full",
                "lang": "en",
            }
            return httpx.Response(
                200,
                json={
                    "search_id": search_id,
                    "results": [{"tool_id": tool_id, "params": [{"name": "q"}]}],
                },
            )
        if request.url.path.endswith("/tools/by-ids"):
            steps.append("inspect")
            assert payload == {
                "tool_ids": [tool_id],
                "search_id": search_id,
                "session_id": session_id,
                "view": "full",
            }
            return httpx.Response(
                200,
                json={"tools": [{"tool_id": tool_id, "required": ["q"]}]},
            )
        if request.url.path.endswith("/tools/execute"):
            steps.append("call")
            assert request.url.params["tool_id"] == tool_id
            assert payload == {
                "search_id": search_id,
                "session_id": session_id,
                "parameters": {"q": "Shanghai"},
                "respond_with": "full",
            }
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "execution_id": "exec_fixture_001",
                    "result": {"temp": 28},
                },
            )
        raise AssertionError(f"unexpected path: {request.url.path}")

    app = create_app(make_settings(), transport=httpx.MockTransport(upstream))
    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://proxy.test"
        ) as client:
            discover = await client.post(
                "/api/v1/search",
                headers=auth_headers(),
                json={
                    "query": "current weather capability",
                    "limit": 5,
                    "session_id": session_id,
                    "view": "full",
                    "lang": "en",
                },
            )
            inspect = await client.post(
                "/api/v1/tools/by-ids",
                headers=auth_headers(),
                json={
                    "tool_ids": [tool_id],
                    "search_id": discover.json()["search_id"],
                    "session_id": session_id,
                    "view": "full",
                },
            )
            call = await client.post(
                f"/api/v1/tools/execute?tool_id={tool_id}",
                headers=auth_headers(),
                json={
                    "search_id": search_id,
                    "session_id": session_id,
                    "parameters": {"q": "Shanghai"},
                    "respond_with": "full",
                },
            )

    assert discover.status_code == 200
    assert inspect.status_code == 200
    assert call.status_code == 200
    assert call.json()["result"] == {"temp": 28}
    assert steps == ["discover", "inspect", "call"]


@pytest.mark.asyncio
async def test_body_size_limit_prevents_upstream_call() -> None:
    calls = 0

    async def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200)

    settings = make_settings(max_request_body_bytes=1024)
    app = create_app(settings, transport=httpx.MockTransport(upstream))
    response = await request_app(
        app,
        "POST",
        "/api/v1/search",
        headers=auth_headers(),
        content=b"x" * 1025,
    )

    assert response.status_code == 413
    assert calls == 0


@pytest.mark.asyncio
async def test_unknown_path_is_not_an_open_proxy() -> None:
    calls = 0

    async def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200)

    app = create_app(make_settings(), transport=httpx.MockTransport(upstream))
    response = await request_app(
        app,
        "GET",
        "/api/v1/http:%2F%2F169.254.169.254/latest/meta-data",
        headers=auth_headers(),
    )

    assert response.status_code == 404
    assert calls == 0


def write_accounts_file(path: Path, account_id: str, api_key: str) -> None:
    path.write_text(
        json.dumps(
            {
                "accounts": [
                    {
                        "id": account_id,
                        "requests_per_minute": 10_000,
                        "burst": 10_000,
                        "keys": [{"id": "primary", "api_key": api_key}],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_accounts_file_can_be_atomically_hot_reloaded(tmp_path: Path) -> None:
    accounts_path = tmp_path / "accounts.json"
    write_accounts_file(accounts_path, "account-a", KEY_A1)
    seen_authorization: list[str] = []

    async def upstream(request: httpx.Request) -> httpx.Response:
        seen_authorization.append(request.headers["authorization"])
        return httpx.Response(200, json={"ok": True})

    settings = make_settings(
        accounts_file_path=str(accounts_path),
        accounts_reload_interval_seconds=0,
    )
    app = create_app(settings, transport=httpx.MockTransport(upstream))
    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://proxy.test",
        ) as client:
            first = await client.post(
                "/api/v1/search",
                headers=auth_headers(account=None),
                json={"query": "first"},
            )
            write_accounts_file(accounts_path, "account-b", KEY_B1)
            reloaded = await client.post(
                "/admin/v1/reload-accounts",
                headers=auth_headers(account=None),
            )
            second = await client.post(
                "/api/v1/search",
                headers=auth_headers(account=None),
                json={"query": "second"},
            )

    assert first.headers["x-qveris-proxy-account"] == "account-a"
    assert reloaded.status_code == 200
    assert reloaded.json()["reload"]["generation"] == 2
    assert second.headers["x-qveris-proxy-account"] == "account-b"
    assert seen_authorization == [f"Bearer {KEY_A1}", f"Bearer {KEY_B1}"]


@pytest.mark.asyncio
async def test_failed_hot_reload_keeps_last_valid_accounts(tmp_path: Path) -> None:
    accounts_path = tmp_path / "accounts.json"
    write_accounts_file(accounts_path, "account-a", KEY_A1)
    seen_authorization: list[str] = []

    async def upstream(request: httpx.Request) -> httpx.Response:
        seen_authorization.append(request.headers["authorization"])
        return httpx.Response(200, json={"ok": True})

    settings = make_settings(
        accounts_file_path=str(accounts_path),
        accounts_reload_interval_seconds=0,
    )
    app = create_app(settings, transport=httpx.MockTransport(upstream))
    async with LifespanManager(app):
        accounts_path.write_text("invalid-secret-marker", encoding="utf-8")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://proxy.test",
        ) as client:
            failed = await client.post(
                "/admin/v1/reload-accounts",
                headers=auth_headers(account=None),
            )
            response = await client.post(
                "/api/v1/search",
                headers=auth_headers(account=None),
                json={"query": "still-valid"},
            )

    assert failed.status_code == 409
    assert "invalid-secret-marker" not in failed.text
    assert response.status_code == 200
    assert seen_authorization == [f"Bearer {KEY_A1}"]


@pytest.mark.asyncio
async def test_account_http_profile_cannot_be_overridden_by_client_headers() -> None:
    captured: httpx.Request | None = None

    async def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal captured
        captured = request
        return httpx.Response(200, json={"ok": True})

    account = AccountConfig(
        id="profile-a",
        requests_per_minute=10_000,
        burst=10_000,
        transport=HTTPTransportConfig(
            user_agent="stable-profile/1.0",
            accept_language="zh-CN,zh;q=0.9",
        ),
        keys=(APIKeyConfig(id="primary", api_key=KEY_A1),),
    )
    settings = make_settings(accounts=(account,))
    app = create_app(settings, transport=httpx.MockTransport(upstream))
    response = await request_app(
        app,
        "POST",
        "/api/v1/search",
        headers={
            **auth_headers("profile-a"),
            "User-Agent": "rotating-client/9.9",
            "Accept-Language": "xx-INVALID",
        },
        json={"query": "profile"},
    )

    assert response.status_code == 200
    assert captured is not None
    assert captured.headers["user-agent"] == "stable-profile/1.0"
    assert captured.headers["accept-language"] == "zh-CN,zh;q=0.9"


class _BlockingResponseStream(httpx.AsyncByteStream):
    def __init__(self, started: asyncio.Event, release: asyncio.Event) -> None:
        self._started = started
        self._release = release

    async def __aiter__(self):
        self._started.set()
        yield b'{"status":"'
        await self._release.wait()
        yield b'ok"}'


class _BlockingCountedResponseStream(httpx.AsyncByteStream):
    def __init__(self, started: asyncio.Event, release: asyncio.Event) -> None:
        self._started = started
        self._release = release
        self.close_calls = 0

    async def __aiter__(self):
        yield b'{"status":"'
        self._started.set()
        await self._release.wait()
        yield b'ok"}'

    async def aclose(self) -> None:
        self.close_calls += 1


class _CloseCountingSingleChunk(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.close_calls = 0

    async def __aiter__(self):
        yield b'{"partial":true}'

    async def aclose(self) -> None:
        self.close_calls += 1


async def _call_with_broken_downstream_send(app, secret: str) -> None:
    body = json.dumps({"query": "send-failure"}).encode()
    delivered = False

    async def receive():
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.body" and message.get("body"):
            raise OSError("downstream send fixture")

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/v1/search",
        "raw_path": b"/api/v1/search",
        "query_string": b"",
        "root_path": "",
        "server": ("proxy.test", 80),
        "client": ("127.0.0.1", 1234),
        "headers": [
            (b"host", b"proxy.test"),
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
            (b"authorization", f"Bearer {secret}".encode()),
            (b"x-qveris-account", b"account-a"),
        ],
    }
    await app(scope, receive, send)


@pytest.mark.asyncio
async def test_proxy_key_concurrency_is_held_until_stream_finishes() -> None:
    stream_started = asyncio.Event()
    release_stream = asyncio.Event()
    stream = _BlockingCountedResponseStream(stream_started, release_stream)
    calls = 0

    async def upstream(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                200,
                headers={"Content-Type": "application/json"},
                stream=stream,
            )
        return httpx.Response(200, json={"ok": True})

    app = create_app(make_settings(), transport=httpx.MockTransport(upstream))
    async with LifespanManager(app):
        manager = app.state.proxy_service.proxy_access_keys
        created = await manager.create("stream client", max_concurrency=1)
        headers = {
            "Authorization": f"Bearer {created.secret}",
            "X-QVeris-Account": "account-a",
        }
        async with (
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://proxy.test",
            ) as slow_client,
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://proxy.test",
            ) as control_client,
        ):
            slow = asyncio.create_task(
                slow_client.post(
                    "/api/v1/search",
                    headers=headers,
                    json={"query": "slow"},
                )
            )
            try:
                await asyncio.wait_for(stream_started.wait(), timeout=1)
                assert (await manager.get(created.key.id)).active_requests == 1

                blocked = await asyncio.wait_for(
                    control_client.post(
                        "/api/v1/search",
                        headers=headers,
                        json={"query": "blocked"},
                    ),
                    timeout=1,
                )
                assert blocked.status_code == 429
                assert blocked.json() == {
                    "detail": "proxy API key concurrency limit reached"
                }
                assert blocked.headers["retry-after"] == "1"
                assert calls == 1
                assert (await manager.get(created.key.id)).requests_used == 1
            finally:
                release_stream.set()

            completed = await asyncio.wait_for(slow, timeout=1)
            assert completed.status_code == 200
            assert completed.json() == {"status": "ok"}
            assert stream.close_calls == 1
            assert (await manager.get(created.key.id)).active_requests == 0

            resumed = await control_client.post(
                "/api/v1/search",
                headers=headers,
                json={"query": "resumed"},
            )
            assert resumed.status_code == 200
            assert calls == 2
            current = await manager.get(created.key.id)
            assert current.requests_used == 2
            assert current.active_requests == 0


@pytest.mark.asyncio
async def test_upstream_connect_error_releases_proxy_key_concurrency() -> None:
    calls = 0

    async def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("fixture unavailable", request=request)
        return httpx.Response(200, json={"ok": True})

    app = create_app(
        make_settings(multiple_accounts=True),
        transport=httpx.MockTransport(upstream),
    )
    async with LifespanManager(app):
        manager = app.state.proxy_service.proxy_access_keys
        created = await manager.create("error client", max_concurrency=1)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://proxy.test",
        ) as client:
            failed = await client.post(
                "/api/v1/search",
                headers={
                    "Authorization": f"Bearer {created.secret}",
                    "X-QVeris-Account": "account-a",
                },
                json={"query": "fails"},
            )
            assert failed.status_code == 502
            assert (await manager.get(created.key.id)).active_requests == 0

            recovered = await client.post(
                "/api/v1/search",
                headers={
                    "Authorization": f"Bearer {created.secret}",
                    "X-QVeris-Account": "account-b",
                },
                json={"query": "works"},
            )
            assert recovered.status_code == 200
            assert calls == 2
            current = await manager.get(created.key.id)
            assert current.requests_used == 2
            assert current.active_requests == 0


@pytest.mark.asyncio
async def test_downstream_send_failure_closes_stream_and_releases_proxy_key() -> None:
    stream = _CloseCountingSingleChunk()

    async def upstream(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream)

    app = create_app(make_settings(), transport=httpx.MockTransport(upstream))
    async with LifespanManager(app):
        manager = app.state.proxy_service.proxy_access_keys
        created = await manager.create("send failure", max_concurrency=1)

        with pytest.raises(ClientDisconnect):
            await _call_with_broken_downstream_send(app, created.secret)

        assert (await manager.get(created.key.id)).active_requests == 0
        assert stream.close_calls == 1
        replacement = await manager.acquire(created.secret)
        await replacement.release()


@pytest.mark.asyncio
async def test_slow_stream_does_not_block_reload_or_new_generation(
    tmp_path: Path,
) -> None:
    accounts_path = tmp_path / "accounts.json"
    write_accounts_file(accounts_path, "account-a", KEY_A1)
    stream_started = asyncio.Event()
    release_stream = asyncio.Event()
    calls = 0

    async def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                200,
                headers={"Content-Type": "application/json"},
                stream=_BlockingResponseStream(stream_started, release_stream),
            )
        return httpx.Response(200, json={"ok": True})

    settings = make_settings(
        accounts_file_path=str(accounts_path),
        accounts_reload_interval_seconds=0,
    )
    app = create_app(settings, transport=httpx.MockTransport(upstream))
    async with LifespanManager(app):
        async with (
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://proxy.test",
            ) as slow_client,
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://proxy.test",
            ) as control_client,
        ):
            slow = asyncio.create_task(
                slow_client.post(
                    "/api/v1/search",
                    headers=auth_headers("account-a"),
                    json={"query": "slow"},
                )
            )
            await asyncio.wait_for(stream_started.wait(), timeout=1)

            write_accounts_file(accounts_path, "account-b", KEY_B1)
            reloaded = await asyncio.wait_for(
                control_client.post(
                    "/admin/v1/reload-accounts",
                    headers=auth_headers(account=None),
                ),
                timeout=1,
            )
            current = await asyncio.wait_for(
                control_client.post(
                    "/api/v1/search",
                    headers=auth_headers("account-b"),
                    json={"query": "current"},
                ),
                timeout=1,
            )

            release_stream.set()
            original = await asyncio.wait_for(slow, timeout=1)

    assert reloaded.status_code == 200
    assert current.status_code == 200
    assert current.headers["x-qveris-proxy-account"] == "account-b"
    assert original.status_code == 200
    assert original.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_hot_reload_preserves_rate_budget_and_key_cursor(tmp_path: Path) -> None:
    accounts_path = tmp_path / "accounts.json"

    def write(include_second_account: bool) -> None:
        accounts = [
            {
                "id": "account-a",
                "requests_per_minute": 1,
                "burst": 2,
                "keys": [
                    {"id": "primary", "api_key": KEY_A1},
                    {"id": "standby", "api_key": KEY_A2},
                ],
            }
        ]
        if include_second_account:
            accounts.append(
                {
                    "id": "account-b",
                    "requests_per_minute": 10_000,
                    "burst": 10_000,
                    "keys": [{"id": "primary", "api_key": KEY_B1}],
                }
            )
        accounts_path.write_text(
            json.dumps({"accounts": accounts}),
            encoding="utf-8",
        )

    write(False)
    authorizations: list[str] = []

    async def upstream(request: httpx.Request) -> httpx.Response:
        authorizations.append(request.headers["authorization"])
        return httpx.Response(200, json={"ok": True})

    settings = make_settings(
        accounts_file_path=str(accounts_path),
        accounts_reload_interval_seconds=0,
    )
    app = create_app(settings, transport=httpx.MockTransport(upstream))
    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://proxy.test",
        ) as client:
            first = await client.post(
                "/api/v1/search",
                headers=auth_headers("account-a"),
                json={"query": "first"},
            )
            write(True)
            reloaded = await client.post(
                "/admin/v1/reload-accounts",
                headers=auth_headers(account=None),
            )
            second = await client.post(
                "/api/v1/search",
                headers=auth_headers("account-a"),
                json={"query": "second"},
            )
            limited = await client.post(
                "/api/v1/search",
                headers=auth_headers("account-a"),
                json={"query": "limited"},
            )

    assert first.status_code == 200
    assert reloaded.status_code == 200
    assert second.status_code == 200
    assert limited.status_code == 429
    assert authorizations == [f"Bearer {KEY_A1}", f"Bearer {KEY_A2}"]


@pytest.mark.asyncio
async def test_replacing_credential_with_same_id_does_not_inherit_401_cooldown(
    tmp_path: Path,
) -> None:
    replacement_key = "sentinel-provider-key-account-a-replacement"
    accounts_path = tmp_path / "accounts.json"
    state_path = str(tmp_path / "state.db")
    write_accounts_file(accounts_path, "account-a", KEY_A1)

    async def upstream(request: httpx.Request) -> httpx.Response:
        if request.headers["authorization"] == f"Bearer {KEY_A1}":
            return httpx.Response(401)
        return httpx.Response(200, json={"ok": True})

    settings = make_settings(
        accounts_file_path=str(accounts_path),
        accounts_reload_interval_seconds=0,
        state_path=state_path,
    )
    first_app = create_app(settings, transport=httpx.MockTransport(upstream))
    async with LifespanManager(first_app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=first_app),
            base_url="http://proxy.test",
        ) as client:
            rejected = await client.post(
                "/api/v1/search",
                headers=auth_headers(),
                json={"query": "old"},
            )
            write_accounts_file(accounts_path, "account-a", replacement_key)
            reloaded = await client.post(
                "/admin/v1/reload-accounts",
                headers=auth_headers(account=None),
            )
            accepted = await client.post(
                "/api/v1/search",
                headers=auth_headers(),
                json={"query": "new"},
            )

    assert rejected.status_code == 401
    assert reloaded.status_code == 200
    assert accepted.status_code == 200

    second_app = create_app(settings, transport=httpx.MockTransport(upstream))
    async with LifespanManager(second_app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=second_app),
            base_url="http://proxy.test",
        ) as client:
            after_restart = await client.post(
                "/api/v1/search",
                headers=auth_headers(),
                json={"query": "after-restart"},
            )

    assert after_restart.status_code == 200


@pytest.mark.asyncio
async def test_injected_shared_transport_is_closed_once() -> None:
    class CountingTransport(httpx.AsyncBaseTransport):
        def __init__(self) -> None:
            self.close_calls = 0

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"ok": True})

        async def aclose(self) -> None:
            self.close_calls += 1

    transport = CountingTransport()
    app = create_app(
        make_settings(multiple_accounts=True),
        transport=transport,
    )
    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://proxy.test",
        ) as client:
            response = await client.get("/api/v1/meta")
            assert response.status_code == 200

    assert transport.close_calls == 1


@pytest.mark.asyncio
async def test_state_persistence_error_does_not_mask_transport_response() -> None:
    async def unavailable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("fixture unavailable", request=request)

    async def fail_to_persist(record) -> None:
        del record
        raise RuntimeError("fixture persistence failure")

    app = create_app(
        make_settings(),
        transport=httpx.MockTransport(unavailable),
    )
    async with LifespanManager(app):
        app.state.proxy_service.state.save_cooldown = fail_to_persist
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://proxy.test",
        ) as client:
            response = await client.post(
                "/api/v1/search",
                headers=auth_headers(),
                json={"query": "fixture"},
            )

    assert response.status_code == 502
    assert response.json() == {"detail": "QVeris upstream is unavailable"}
