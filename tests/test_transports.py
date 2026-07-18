from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from qveris_proxy.transports import (
    AccountTransportManager,
    AccountTransportSpec,
    HTTPProfile,
    TransportConfigurationError,
    TransportManagerClosed,
)


class _ControlledCloseTransport(httpx.AsyncBaseTransport):
    def __init__(
        self,
        *,
        close_failures: int = 0,
        close_gate: asyncio.Event | None = None,
    ) -> None:
        self.close_failures = close_failures
        self.close_gate = close_gate
        self.close_started = asyncio.Event()
        self.close_calls = 0
        self.closed = False

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"url": str(request.url)})

    async def aclose(self) -> None:
        self.close_calls += 1
        self.close_started.set()
        if self.close_gate is not None:
            await self.close_gate.wait()
        if self.close_failures > 0:
            self.close_failures -= 1
            raise RuntimeError("controlled transport close failure")
        self.closed = True


@pytest.mark.asyncio
async def test_account_clients_have_isolated_transports_and_profiles() -> None:
    calls: dict[str, list[httpx.Request]] = {
        "account-a": [],
        "account-b": [],
        "public": [],
    }

    def mock_for(label: str) -> httpx.MockTransport:
        async def handle(request: httpx.Request) -> httpx.Response:
            calls[label].append(request)
            return httpx.Response(200, json={"transport": label})

        return httpx.MockTransport(handle)

    transports = {
        "account-a": mock_for("account-a"),
        "account-b": mock_for("account-b"),
    }
    specs = [
        AccountTransportSpec(
            "account-a",
            HTTPProfile("profile-a/1.0", "zh-CN,zh;q=0.9"),
        ),
        AccountTransportSpec(
            "account-b",
            HTTPProfile("profile-b/1.0", "en-US,en;q=0.8"),
        ),
    ]
    manager = await AccountTransportManager.create(
        specs,
        base_url="https://upstream.test/api/",
        public_profile=HTTPProfile("public-profile/1.0", "en-GB,en;q=0.7"),
        transport_factory=lambda spec: transports[spec.account_id],
        public_transport=mock_for("public"),
    )

    try:
        client_a = manager.client_for("account-a")
        client_b = manager.client_for("account-b")
        assert client_a is not client_b

        response_a = await client_a.get("credits")
        response_b = await client_b.get("credits")
        public_response = await manager.public_client.get("meta")

        assert response_a.json() == {"transport": "account-a"}
        assert response_b.json() == {"transport": "account-b"}
        assert public_response.json() == {"transport": "public"}
        assert len(calls["account-a"]) == 1
        assert len(calls["account-b"]) == 1
        assert len(calls["public"]) == 1
        assert calls["account-a"][0].headers["user-agent"] == "profile-a/1.0"
        assert calls["account-a"][0].headers["accept-language"] == "zh-CN,zh;q=0.9"
        assert calls["account-b"][0].headers["user-agent"] == "profile-b/1.0"
        assert calls["account-b"][0].headers["accept-language"] == "en-US,en;q=0.8"
        assert calls["public"][0].headers["user-agent"] == "public-profile/1.0"
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_proxy_reference_errors_do_not_disclose_path_or_content(
    tmp_path: Path,
) -> None:
    missing_marker = "secret-path-marker"
    missing = tmp_path / missing_marker
    manager = AccountTransportManager()
    try:
        with pytest.raises(TransportConfigurationError) as unavailable:
            await manager.reload(
                [AccountTransportSpec("account-a", proxy_url_file=missing)]
            )
        assert str(unavailable.value) == "proxy URL file is unavailable"
        assert missing_marker not in repr(unavailable.value)

        content_marker = "proxy-password-marker"
        invalid = tmp_path / "proxy-reference"
        invalid.write_text(
            f"ftp://user:{content_marker}@proxy.test:8080",
            encoding="utf-8",
        )
        with pytest.raises(TransportConfigurationError) as malformed:
            await manager.reload(
                [AccountTransportSpec("account-a", proxy_url_file=invalid)]
            )
        assert str(malformed.value) == "proxy URL file is invalid"
        assert content_marker not in repr(malformed.value)
        assert manager.account_ids() == ()
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_reload_adds_removes_replaces_and_retains_clients() -> None:
    generation = 0

    def transport_factory(spec: AccountTransportSpec) -> httpx.MockTransport:
        nonlocal generation
        generation += 1
        label = f"{spec.account_id}-{generation}"
        return httpx.MockTransport(
            lambda request: httpx.Response(200, json={"transport": label})
        )

    manager = await AccountTransportManager.create(
        [AccountTransportSpec("account-a"), AccountTransportSpec("account-b")],
        base_url="https://upstream.test/",
        transport_factory=transport_factory,
        public_transport=httpx.MockTransport(lambda request: httpx.Response(200)),
    )
    public_client = manager.public_client
    old_a = manager.client_for("account-a")
    old_b = manager.client_for("account-b")

    result = await manager.reload(
        [AccountTransportSpec("account-b"), AccountTransportSpec("account-c")]
    )
    assert result.added == ("account-c",)
    assert result.removed == ("account-a",)
    assert result.replaced == ()
    assert result.retained == ("account-b",)
    assert old_a.is_closed
    assert manager.client_for("account-b") is old_b
    assert not old_b.is_closed
    client_c = manager.client_for("account-c")

    result = await manager.reload(
        [
            AccountTransportSpec(
                "account-b", HTTPProfile("changed-profile/1.0", "en-US")
            ),
            AccountTransportSpec("account-c"),
        ]
    )
    assert result.added == ()
    assert result.removed == ()
    assert result.replaced == ("account-b",)
    assert result.retained == ("account-c",)
    assert old_b.is_closed
    assert manager.client_for("account-c") is client_c

    replacement_b = manager.client_for("account-b")
    await manager.aclose()
    await manager.aclose()

    assert manager.closed
    assert replacement_b.is_closed
    assert client_c.is_closed
    assert public_client.is_closed
    with pytest.raises(TransportManagerClosed):
        manager.client_for("account-b")


@pytest.mark.asyncio
async def test_proxy_file_change_replaces_only_affected_pool(tmp_path: Path) -> None:
    proxy_file = tmp_path / "proxy-url"
    proxy_file.write_text("http://proxy-a.test:8080\n", encoding="utf-8")
    spec = AccountTransportSpec("account-a", proxy_url_file=proxy_file)
    manager = await AccountTransportManager.create([spec])

    try:
        first = manager.client_for("account-a")
        assert manager.proxy_configured_for("account-a")

        unchanged = await manager.reload([spec])
        assert unchanged.retained == ("account-a",)
        assert manager.client_for("account-a") is first

        proxy_file.write_text("http://proxy-b.test:8080\n", encoding="utf-8")
        changed = await manager.reload([spec])
        assert changed.replaced == ("account-a",)
        assert first.is_closed
        assert manager.client_for("account-a") is not first
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_reload_defers_retired_close_until_last_lease_releases() -> None:
    generation = 0

    def transport_factory(spec: AccountTransportSpec) -> httpx.MockTransport:
        nonlocal generation
        generation += 1
        label = f"{spec.account_id}-{generation}"
        return httpx.MockTransport(
            lambda request: httpx.Response(200, json={"transport": label})
        )

    manager = await AccountTransportManager.create(
        [AccountTransportSpec("account-a")],
        base_url="https://upstream.test/",
        transport_factory=transport_factory,
    )
    first_lease = await manager.acquire("account-a")
    second_lease = await manager.acquire("account-a")
    retired_client = first_lease.client

    try:
        result = await manager.reload(
            [
                AccountTransportSpec(
                    "account-a",
                    HTTPProfile("replacement-profile/1.0", "en-US"),
                )
            ]
        )
        assert result.replaced == ("account-a",)
        assert manager.client_for("account-a") is not retired_client
        assert not retired_client.is_closed

        await first_lease.release()
        assert not retired_client.is_closed
        response = await second_lease.client.get("credits")
        assert response.json() == {"transport": "account-a-1"}

        await second_lease.release()
        assert retired_client.is_closed
    finally:
        await first_lease.release()
        await second_lease.release()
        await manager.aclose()


@pytest.mark.asyncio
async def test_reload_commits_when_retired_client_close_fails() -> None:
    retired_transport = _ControlledCloseTransport(close_failures=1)
    replacement_transport = _ControlledCloseTransport()
    transports = iter((retired_transport, replacement_transport))
    manager = await AccountTransportManager.create(
        [AccountTransportSpec("account-a")],
        transport_factory=lambda spec: next(transports),
    )
    retired_client = manager.client_for("account-a")

    try:
        result = await manager.reload(
            [
                AccountTransportSpec(
                    "account-a",
                    HTTPProfile("replacement-profile/1.0", "en-US"),
                )
            ]
        )

        assert result.replaced == ("account-a",)
        assert manager.client_for("account-a") is not retired_client
        assert retired_transport.close_calls == 1
        assert not retired_transport.closed

        await manager.aclose()
        assert retired_transport.close_calls == 2
        assert retired_transport.closed
        assert replacement_transport.closed
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_cancelled_reload_keeps_retired_cleanup_owned_by_manager() -> None:
    close_gate = asyncio.Event()
    retired_transport = _ControlledCloseTransport(close_gate=close_gate)
    replacement_transport = _ControlledCloseTransport()
    transports = iter((retired_transport, replacement_transport))
    manager = await AccountTransportManager.create(
        [AccountTransportSpec("account-a")],
        transport_factory=lambda spec: next(transports),
    )
    retired_client = manager.client_for("account-a")
    reload_task = asyncio.create_task(
        manager.reload(
            [
                AccountTransportSpec(
                    "account-a",
                    HTTPProfile("replacement-profile/1.0", "en-US"),
                )
            ]
        )
    )
    close_task: asyncio.Task[None] | None = None

    try:
        await asyncio.wait_for(retired_transport.close_started.wait(), timeout=1)
        replacement_client = manager.client_for("account-a")
        assert replacement_client is not retired_client

        reload_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await reload_task

        close_task = asyncio.create_task(manager.aclose())
        await asyncio.sleep(0)
        assert not close_task.done()
        assert not retired_transport.closed

        close_gate.set()
        await asyncio.wait_for(close_task, timeout=1)
        assert retired_transport.close_calls == 1
        assert retired_transport.closed
        assert replacement_transport.closed
    finally:
        close_gate.set()
        if not reload_task.done():
            reload_task.cancel()
        await asyncio.gather(reload_task, return_exceptions=True)
        if close_task is not None:
            await asyncio.gather(close_task, return_exceptions=True)
        await manager.aclose()


@pytest.mark.asyncio
async def test_manager_close_waits_for_active_account_lease() -> None:
    manager = await AccountTransportManager.create(
        [AccountTransportSpec("account-a")],
        transport_factory=lambda spec: httpx.MockTransport(
            lambda request: httpx.Response(200)
        ),
    )
    lease = await manager.acquire("account-a")
    account_client = lease.client
    public_client = manager.public_client
    close_task = asyncio.create_task(manager.aclose())

    try:
        await asyncio.sleep(0)
        assert manager.closed
        assert not close_task.done()
        assert not account_client.is_closed

        await lease.release()
        await asyncio.wait_for(close_task, timeout=1)
        assert account_client.is_closed
        assert public_client.is_closed
    finally:
        await lease.release()
        await asyncio.wait_for(manager.aclose(), timeout=1)


@pytest.mark.asyncio
async def test_manager_close_waits_for_active_public_lease() -> None:
    public_transport = _ControlledCloseTransport()
    manager = await AccountTransportManager.create(
        [],
        public_transport=public_transport,
    )
    lease = await manager.acquire_public()
    close_task = asyncio.create_task(manager.aclose())

    try:
        await asyncio.sleep(0)
        assert manager.closed
        assert not close_task.done()
        assert public_transport.close_calls == 0

        await lease.release()
        await asyncio.wait_for(close_task, timeout=1)
        assert public_transport.close_calls == 1
        assert public_transport.closed
    finally:
        await lease.release()
        await asyncio.wait_for(manager.aclose(), timeout=1)


@pytest.mark.asyncio
async def test_failed_reload_keeps_existing_clients(tmp_path: Path) -> None:
    manager = await AccountTransportManager.create(
        [AccountTransportSpec("account-a")],
        transport_factory=lambda spec: httpx.MockTransport(
            lambda request: httpx.Response(200)
        ),
    )
    existing = manager.client_for("account-a")
    invalid = tmp_path / "invalid-proxy"
    invalid.write_text("not-a-proxy-url", encoding="utf-8")

    try:
        with pytest.raises(TransportConfigurationError):
            await manager.reload(
                [
                    AccountTransportSpec("account-a"),
                    AccountTransportSpec("account-b", proxy_url_file=invalid),
                ]
            )
        assert manager.account_ids() == ("account-a",)
        assert manager.client_for("account-a") is existing
        assert not existing.is_closed
    finally:
        await manager.aclose()
