from __future__ import annotations

import asyncio

import pytest

from qveris_proxy.gate import ConfigurationGate


@pytest.mark.asyncio
async def test_waiting_update_keeps_reads_flowing_then_blocks_during_commit() -> None:
    gate = ConfigurationGate()
    first = await gate.acquire()
    update_entered = asyncio.Event()
    allow_update_to_finish = asyncio.Event()

    async def update() -> None:
        async with gate.update():
            update_entered.set()
            await allow_update_to_finish.wait()

    update_task = asyncio.create_task(update())
    await asyncio.sleep(0)
    second = await asyncio.wait_for(gate.acquire(), timeout=1)

    assert not update_entered.is_set()

    await first.release()
    assert not update_entered.is_set()
    await second.release()
    await asyncio.wait_for(update_entered.wait(), timeout=1)

    blocked_reader = asyncio.create_task(gate.acquire())
    await asyncio.sleep(0)
    assert not blocked_reader.done()

    allow_update_to_finish.set()
    await asyncio.wait_for(update_task, timeout=1)
    reader = await asyncio.wait_for(blocked_reader, timeout=1)
    await reader.release()

    assert await gate.status() == {
        "active": 0,
        "updating": False,
        "waiting_updates": 0,
    }


@pytest.mark.asyncio
async def test_cancelled_update_restores_gate() -> None:
    gate = ConfigurationGate()
    active = await gate.acquire()
    update_task = asyncio.create_task(gate.update().__aenter__())
    await asyncio.sleep(0)

    update_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await update_task
    await active.release()

    replacement = await asyncio.wait_for(gate.acquire(), timeout=1)
    await replacement.release()

    assert (await gate.status())["updating"] is False


@pytest.mark.asyncio
async def test_lease_release_is_idempotent() -> None:
    gate = ConfigurationGate()
    lease = await gate.acquire()

    await asyncio.gather(lease.release(), lease.release())

    assert (await gate.status())["active"] == 0
