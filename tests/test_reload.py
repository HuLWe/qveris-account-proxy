from __future__ import annotations

import asyncio

import pytest

from qveris_proxy.reload import CredentialFileReloader, ReloadError


@pytest.mark.asyncio
async def test_prime_and_unchanged_reload_do_not_reapply(tmp_path) -> None:
    path = tmp_path / "accounts.json"
    path.write_bytes(b'{"accounts":[]}')
    applied: list[bytes] = []

    async def apply(payload: bytes) -> None:
        applied.append(payload)

    reloader = CredentialFileReloader(path, apply)
    await reloader.prime()

    result = await reloader.reload()
    status = await reloader.status()

    assert result.applied is False
    assert result.changed is False
    assert result.generation == 1
    assert applied == []
    assert status.error is None


@pytest.mark.asyncio
async def test_changed_payload_is_applied_once(tmp_path) -> None:
    path = tmp_path / "accounts.json"
    path.write_bytes(b"first")
    applied: list[bytes] = []

    async def apply(payload: bytes) -> None:
        applied.append(payload)

    reloader = CredentialFileReloader(path, apply)
    await reloader.prime()
    path.write_bytes(b"second")

    changed = await reloader.reload()
    unchanged = await reloader.reload()

    assert changed.applied is True
    assert changed.changed is True
    assert changed.generation == 2
    assert unchanged.applied is False
    assert applied == [b"second"]


@pytest.mark.asyncio
async def test_failed_apply_keeps_previous_generation_and_retries(tmp_path) -> None:
    path = tmp_path / "accounts.json"
    path.write_bytes(b"first")
    attempts = 0

    async def apply(payload: bytes) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ValueError(payload.decode())

    reloader = CredentialFileReloader(path, apply)
    await reloader.prime()
    path.write_bytes(b"secret-new-payload")

    failed = await reloader.reload()
    succeeded = await reloader.reload()

    assert failed.error == "apply_failed"
    assert failed.generation == 1
    assert succeeded.applied is True
    assert succeeded.generation == 2
    assert attempts == 2
    assert "secret-new-payload" not in repr(failed)


@pytest.mark.asyncio
async def test_read_errors_are_sanitized(tmp_path) -> None:
    path = tmp_path / "missing.json"

    async def apply(payload: bytes) -> None:
        raise AssertionError(payload)

    reloader = CredentialFileReloader(path, apply)
    result = await reloader.reload()

    assert result.error == "file_unavailable"
    assert str(path) not in repr(result)


@pytest.mark.asyncio
async def test_prime_rejects_oversized_file(tmp_path) -> None:
    path = tmp_path / "accounts.json"
    path.write_bytes(b"x" * 5)

    async def apply(payload: bytes) -> None:
        raise AssertionError(payload)

    reloader = CredentialFileReloader(path, apply, max_bytes=4)

    with pytest.raises(ReloadError, match="file_too_large"):
        await reloader.prime()


@pytest.mark.asyncio
async def test_background_loop_stops_without_leaking_task(tmp_path) -> None:
    path = tmp_path / "accounts.json"
    path.write_bytes(b"first")
    applied = asyncio.Event()

    async def apply(payload: bytes) -> None:
        assert payload == b"second"
        applied.set()

    reloader = CredentialFileReloader(
        path,
        apply,
        interval_seconds=0.01,
    )
    await reloader.prime()
    path.write_bytes(b"second")
    stop = asyncio.Event()
    task = asyncio.create_task(reloader.run(stop))

    await asyncio.wait_for(applied.wait(), timeout=1)
    stop.set()
    await asyncio.wait_for(task, timeout=1)

    assert task.done()
