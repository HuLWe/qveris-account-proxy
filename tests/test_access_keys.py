from __future__ import annotations

import asyncio
import hashlib
import re
from pathlib import Path

import pytest

from qveris_proxy.access_keys import (
    ProxyAccessKeyManager,
    ProxyAccessKeyRejected,
    generate_proxy_access_key,
    hash_proxy_access_key,
    proxy_access_key_parts,
)
from qveris_proxy.state import StateStore


KEY_PATTERN = re.compile(r"^sk-[A-Za-z0-9_-]{43}$")
LEGACY_KEY = "sk-" + "L" * 43
ROTATED_KEY = "sk-" + "R" * 43
MANAGED_KEY = "sk-" + "M" * 43


class Clock:
    def __init__(self, now: float = 1_800_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_manager(
    store: StateStore,
    *,
    secret: str = MANAGED_KEY,
    key_id: str = "managed-key",
) -> ProxyAccessKeyManager:
    return ProxyAccessKeyManager(
        store,
        secret_factory=lambda: secret,
        id_factory=lambda: key_id,
    )


def test_generated_proxy_access_key_uses_official_shape() -> None:
    first = generate_proxy_access_key()
    second = generate_proxy_access_key()

    assert KEY_PATTERN.fullmatch(first)
    assert KEY_PATTERN.fullmatch(second)
    assert first != second
    assert len(hash_proxy_access_key(first)) == 64
    assert proxy_access_key_parts("0123456789abcdef") == ("0123", "cdef")


@pytest.mark.asyncio
async def test_proxy_access_key_limits_reject_database_overflow_values(
    tmp_path: Path,
) -> None:
    store = StateStore(str(tmp_path / "state.db"))
    manager = make_manager(store)

    with pytest.raises(ValueError):
        await manager.create("Too much usage", request_limit=1_000_000_000_001)
    with pytest.raises(ValueError):
        await manager.create("Too much RPM", requests_per_minute=1_000_001)
    with pytest.raises(ValueError):
        await manager.create("Too much concurrency", max_concurrency=1025)
    with pytest.raises(ValueError):
        await manager.create("Too distant", expires_at=253_402_300_800)

    assert await manager.list() == []
    await store.close()


@pytest.mark.asyncio
async def test_primary_migration_rotates_only_secret_metadata_and_keeps_usage(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "state.db"
    store = StateStore(str(state_path))
    manager = make_manager(store)

    initial = await manager.initialize(LEGACY_KEY, max_concurrency=512)
    assert initial.id == "primary"
    assert initial.kind == "primary"
    assert initial.prefix == "sk-"
    assert initial.max_concurrency == 512
    await manager.update(
        "primary",
        name="Existing clients",
        request_limit=5,
        requests_per_minute=3,
        max_concurrency=4,
    )
    lease = await manager.acquire(LEGACY_KEY)
    await lease.release()

    rotated = await manager.initialize(ROTATED_KEY)
    assert rotated.name == "Existing clients"
    assert rotated.request_limit == 5
    assert rotated.requests_per_minute == 3
    assert rotated.max_concurrency == 4
    assert rotated.requests_used == 1
    assert rotated.suffix == ROTATED_KEY[-4:]
    assert (
        await store.get_proxy_access_key_by_hash(hash_proxy_access_key(LEGACY_KEY))
        is None
    )
    assert await store.get_proxy_access_key_by_hash(hash_proxy_access_key(ROTATED_KEY))

    with pytest.raises(ProxyAccessKeyRejected) as old_token:
        await manager.acquire(LEGACY_KEY)
    assert old_token.value.reason == "invalid"

    await store.close()
    database_bytes = state_path.read_bytes()
    assert LEGACY_KEY.encode() not in database_bytes
    assert ROTATED_KEY.encode() not in database_bytes
    assert hashlib.sha256(ROTATED_KEY.encode()).hexdigest().encode() in database_bytes


@pytest.mark.asyncio
async def test_primary_key_can_be_deleted_without_reappearing_after_restart(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "state.db"
    store = StateStore(str(state_path))
    manager = make_manager(store)
    await manager.initialize(LEGACY_KEY)

    created = await manager.create("Desktop client")
    assert created.secret == MANAGED_KEY
    assert MANAGED_KEY not in repr(created)
    assert created.key.kind == "managed"
    assert created.key.prefix == "sk-"
    assert created.key.suffix == MANAGED_KEY[-4:]
    assert all(MANAGED_KEY not in repr(key) for key in await manager.list())

    await manager.delete("primary")
    assert [key.id for key in await manager.list()] == [created.key.id]
    with pytest.raises(ProxyAccessKeyRejected) as deleted_primary:
        await manager.acquire(LEGACY_KEY)
    assert deleted_primary.value.reason == "invalid"

    await store.close()
    restarted_store = StateStore(str(state_path))
    restarted_manager = make_manager(restarted_store)
    assert await restarted_manager.initialize(LEGACY_KEY) is None
    assert [key.id for key in await restarted_manager.list()] == [created.key.id]
    with pytest.raises(ProxyAccessKeyRejected) as still_deleted:
        await restarted_manager.acquire(LEGACY_KEY)
    assert still_deleted.value.reason == "invalid"
    await restarted_store.close()

    standalone_store = StateStore(str(tmp_path / "standalone.db"))
    standalone_manager = make_manager(standalone_store)
    standalone_key = await standalone_manager.create("Disposable client")
    await standalone_manager.delete(standalone_key.key.id)
    assert await standalone_manager.list() == []
    await standalone_store.close()


@pytest.mark.asyncio
async def test_inspecting_proxy_key_does_not_consume_usage(tmp_path: Path) -> None:
    store = StateStore(str(tmp_path / "state.db"))
    manager = make_manager(store)
    created = await manager.create("Inspect")

    inspected = await manager.inspect(created.secret)
    assert inspected.id == created.key.id
    assert inspected.requests_used == 0
    assert (await manager.get(created.key.id)).requests_used == 0

    await manager.delete(created.key.id)
    assert await manager.list() == []
    await store.close()


@pytest.mark.asyncio
async def test_request_limit_and_concurrency_are_consumed_once(tmp_path: Path) -> None:
    store = StateStore(str(tmp_path / "state.db"))
    manager = make_manager(store)
    created = await manager.create(
        "Limited",
        request_limit=2,
        max_concurrency=1,
    )

    first = await manager.acquire(created.secret)
    assert first.key.requests_used == 1
    assert first.key.active_requests == 1
    with pytest.raises(ProxyAccessKeyRejected) as busy:
        await manager.acquire(created.secret)
    assert busy.value.reason == "concurrency"
    assert (await manager.get(created.key.id)).requests_used == 1

    await first.release()
    await first.release()
    second = await manager.acquire(created.secret)
    await second.release()
    with pytest.raises(ProxyAccessKeyRejected) as exhausted:
        await manager.acquire(created.secret)
    assert exhausted.value.reason == "request_limit"
    assert (await manager.get(created.key.id)).requests_used == 2
    await store.close()


@pytest.mark.asyncio
async def test_disabled_key_wins_over_concurrency_without_consuming_usage(
    tmp_path: Path,
) -> None:
    store = StateStore(str(tmp_path / "state.db"))
    manager = make_manager(store)
    created = await manager.create("Disable during stream", max_concurrency=1)
    lease = await manager.acquire(created.secret)

    await manager.update(created.key.id, enabled=False)
    with pytest.raises(ProxyAccessKeyRejected) as rejected:
        await manager.acquire(created.secret)
    assert rejected.value.reason == "disabled"
    assert (await manager.get(created.key.id)).requests_used == 1

    await lease.release()
    assert (await manager.get(created.key.id)).active_requests == 0
    await store.close()


@pytest.mark.asyncio
async def test_rpm_is_atomic_and_recovers_after_window(tmp_path: Path) -> None:
    clock = Clock()
    store = StateStore(str(tmp_path / "state.db"), wall_time=clock)
    manager = make_manager(store)
    created = await manager.create("RPM", requests_per_minute=1)

    first = await manager.acquire(created.secret)
    await first.release()
    with pytest.raises(ProxyAccessKeyRejected) as limited:
        await manager.acquire(created.secret)
    assert limited.value.reason == "rate_limit"
    assert limited.value.retry_after == 60
    assert (await manager.get(created.key.id)).requests_used == 1

    clock.advance(60)
    recovered = await manager.acquire(created.secret)
    await recovered.release()
    assert (await manager.get(created.key.id)).requests_used == 2
    await store.close()


@pytest.mark.asyncio
async def test_state_consumption_is_atomic_across_store_instances(
    tmp_path: Path,
) -> None:
    state_path = str(tmp_path / "state.db")
    first_store = StateStore(state_path)
    manager = make_manager(first_store)
    created = await manager.create("Atomic", request_limit=1)
    second_store = StateStore(state_path)
    secret_hash = hash_proxy_access_key(created.secret)

    first, second = await asyncio.gather(
        first_store.consume_proxy_access_key(secret_hash),
        second_store.consume_proxy_access_key(secret_hash),
    )
    assert sorted([first.accepted, second.accepted]) == [False, True]
    rejected = first if not first.accepted else second
    assert rejected.reason == "request_limit"
    assert (await first_store.get_proxy_access_key(created.key.id)).requests_used == 1

    await second_store.close()
    await first_store.close()
