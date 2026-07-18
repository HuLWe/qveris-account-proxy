from __future__ import annotations

import asyncio

import httpx
import pytest

from qveris_proxy.config import APIKeyConfig, OAuthTokenConfig
from qveris_proxy.pool import KeyPool, PoolUnavailable
from qveris_proxy.state import StoredCooldown
from conftest import KEY_A1, KEY_A2, OAUTH_A1, make_settings

REPLACEMENT_KEY = "sentinel-provider-key-account-a-replacement"
OAUTH_A2 = "sentinel-oauth-token-account-a-standby"
REPLACEMENT_OAUTH = "sentinel-oauth-token-account-a-replacement"


class Clock:
    def __init__(self) -> None:
        self.value = 100.0
        self.wall = 1_700_000_000.0

    def monotonic(self) -> float:
        return self.value

    def wall_time(self) -> float:
        return self.wall

    def advance(self, seconds: float) -> None:
        self.value += seconds
        self.wall += seconds


def settings_with_rate_limit(requests_per_minute: float, burst: int, **overrides):
    settings = make_settings(**overrides)
    account = settings.accounts[0].model_copy(
        update={
            "requests_per_minute": requests_per_minute,
            "burst": burst,
        }
    )
    return settings.model_copy(update={"accounts": (account,)})


@pytest.mark.asyncio
async def test_round_robin_within_one_account() -> None:
    pool = KeyPool(make_settings())
    first = await pool.acquire("account-a", "search")
    await first.release()
    second = await pool.acquire("account-a", "search")
    await second.release()

    assert first.api_key == KEY_A1
    assert second.api_key == KEY_A2


@pytest.mark.asyncio
async def test_cancelled_release_still_returns_inflight_capacity() -> None:
    settings = make_settings(max_inflight_per_key=1)
    account = settings.accounts[0].model_copy(
        update={"keys": (settings.accounts[0].keys[0],), "oauth_tokens": ()}
    )
    pool = KeyPool(settings.model_copy(update={"accounts": (account,)}))
    lease = await pool.acquire("account-a", "search")

    await pool._lock.acquire()
    try:
        release = asyncio.create_task(lease.release())
        await asyncio.sleep(0)
        release.cancel()
        with pytest.raises(asyncio.CancelledError):
            await release
    finally:
        pool._lock.release()

    await asyncio.wait_for(lease.release(), timeout=1)
    replacement = await asyncio.wait_for(pool.acquire("account-a", "search"), timeout=1)
    await replacement.release()


@pytest.mark.asyncio
async def test_runtime_migration_preserves_account_and_credential_cursors() -> None:
    clock = Clock()
    settings = make_settings(multiple_accounts=True, routing_mode="round_robin")
    accounts = tuple(
        account.model_copy(update={"requests_per_minute": 60, "burst": 2})
        for account in settings.accounts
    )
    settings = settings.model_copy(update={"accounts": accounts})
    previous = KeyPool(settings, monotonic=clock.monotonic, wall_time=clock.wall_time)

    first = await previous.acquire_any("search")
    assert first.account_id == "account-a"
    assert first.api_key == KEY_A1
    await first.release()

    replacement = KeyPool(
        settings, monotonic=clock.monotonic, wall_time=clock.wall_time
    )
    await replacement.migrate_runtime_from(previous)

    status = {item["id"]: item for item in await replacement.status()}
    assert status["account-a"]["rate_limit"]["available_tokens"] == 1.0

    next_account = await replacement.acquire_any("search")
    assert next_account.account_id == "account-b"
    await next_account.release()

    next_key = await replacement.acquire("account-a", "search")
    assert next_key.api_key == KEY_A2
    await next_key.release()
    with pytest.raises(PoolUnavailable) as captured:
        await replacement.acquire("account-a", "search")
    assert captured.value.reason == "rate_limit"


@pytest.mark.asyncio
async def test_runtime_migration_matches_credential_cooldown_by_fingerprint() -> None:
    clock = Clock()
    settings = make_settings(auth_failure_cooldown_seconds=60)
    account = settings.accounts[0].model_copy(
        update={
            "oauth_tokens": (
                settings.accounts[0].oauth_tokens[0],
                OAuthTokenConfig(id="standby", access_token=OAUTH_A2),
            )
        }
    )
    settings = settings.model_copy(update={"accounts": (account,)})
    previous = KeyPool(settings, monotonic=clock.monotonic, wall_time=clock.wall_time)
    records = []
    for credential_kind in ("api_key", "api_key", "oauth", "oauth"):
        lease = await previous.acquire(
            "account-a", "search", credential_kind=credential_kind
        )
        record = await previous.report_response(lease, 401, {})
        assert record is not None
        records.append(record)
        await lease.release()

    replacement_account = account.model_copy(
        update={
            "keys": (
                account.keys[0],
                APIKeyConfig(id="standby", api_key=REPLACEMENT_KEY),
            ),
            "oauth_tokens": (
                OAuthTokenConfig(id="primary", access_token=OAUTH_A1),
                OAuthTokenConfig(id="standby", access_token=REPLACEMENT_OAUTH),
            ),
        }
    )
    replacement_settings = settings.model_copy(
        update={"accounts": (replacement_account,)}
    )
    replacement = KeyPool(
        replacement_settings,
        monotonic=clock.monotonic,
        wall_time=clock.wall_time,
    )

    await replacement.restore_cooldowns(records)
    before = (await replacement.status())[0]
    assert before["available_keys"] == 1
    assert before["available_oauth_tokens"] == 1

    await replacement.migrate_runtime_from(previous)
    after = (await replacement.status())[0]
    assert after["available_keys"] == 1
    assert after["available_oauth_tokens"] == 1

    api_lease = await replacement.acquire("account-a", "search")
    oauth_lease = await replacement.acquire(
        "account-a", "search", credential_kind="oauth"
    )
    assert api_lease.api_key == REPLACEMENT_KEY
    assert oauth_lease.bearer_token == REPLACEMENT_OAUTH
    await api_lease.release()
    await oauth_lease.release()


@pytest.mark.asyncio
async def test_429_cools_entire_account_not_only_selected_key() -> None:
    clock = Clock()
    pool = KeyPool(
        make_settings(), monotonic=clock.monotonic, wall_time=clock.wall_time
    )
    lease = await pool.acquire("account-a", "search")
    await pool.report_response(lease, 429, dict(httpx.Headers({"Retry-After": "20"})))
    await lease.release()

    with pytest.raises(PoolUnavailable) as captured:
        await pool.acquire("account-a", "search")
    assert captured.value.retry_after == 20
    assert captured.value.reason == "rate_limit"

    other_route = await pool.acquire("account-a", "tools/execute")
    await other_route.release()

    clock.advance(20)
    recovered = await pool.acquire("account-a", "search")
    await recovered.release()


@pytest.mark.asyncio
async def test_401_quarantines_only_the_rejected_key() -> None:
    clock = Clock()
    pool = KeyPool(
        make_settings(), monotonic=clock.monotonic, wall_time=clock.wall_time
    )
    rejected = await pool.acquire("account-a", "search")
    await pool.report_response(rejected, 401, {})
    await rejected.release()

    standby = await pool.acquire("account-a", "search")
    assert standby.api_key == KEY_A2
    await standby.release()


@pytest.mark.asyncio
async def test_402_cools_credit_sensitive_routes_but_allows_credit_probe() -> None:
    clock = Clock()
    pool = KeyPool(
        make_settings(payment_required_cooldown_seconds=60),
        monotonic=clock.monotonic,
        wall_time=clock.wall_time,
    )
    lease = await pool.acquire("account-a", "tools/execute", credit_sensitive=True)
    cooldown = await pool.report_response(lease, 402, {})
    await lease.release()

    assert cooldown is not None
    assert cooldown.scope == "account"
    with pytest.raises(PoolUnavailable) as captured:
        await pool.acquire("account-a", "search", credit_sensitive=True)
    assert captured.value.reason == "credits"
    assert captured.value.retry_after == 60

    probe = await pool.acquire("account-a", "auth/credits")
    await probe.release()

    await pool.clear_credit_cooldown("account-a")
    recovered = await pool.acquire("account-a", "search", credit_sensitive=True)
    await recovered.release()


@pytest.mark.asyncio
async def test_account_token_bucket_is_shared_by_all_keys() -> None:
    clock = Clock()
    pool = KeyPool(
        settings_with_rate_limit(60, 2),
        monotonic=clock.monotonic,
        wall_time=clock.wall_time,
    )

    first = await pool.acquire("account-a", "search")
    await first.release()
    second = await pool.acquire("account-a", "tools/by-ids")
    await second.release()

    assert first.api_key != second.api_key
    with pytest.raises(PoolUnavailable) as captured:
        await pool.acquire("account-a", "tools/execute")
    assert captured.value.reason == "rate_limit"
    assert captured.value.retry_after == 1

    clock.advance(1)
    recovered = await pool.acquire("account-a", "tools/execute")
    await recovered.release()


@pytest.mark.asyncio
async def test_quota_control_budget_is_independent_from_business_rate_limit() -> None:
    clock = Clock()
    pool = KeyPool(
        settings_with_rate_limit(1, 1),
        monotonic=clock.monotonic,
        wall_time=clock.wall_time,
    )

    business = await pool.acquire("account-a", "search")
    await business.release()
    for _ in range(4):
        probe = await pool.acquire("account-a", "auth/credits", control=True)
        await probe.release()

    with pytest.raises(PoolUnavailable) as business_limited:
        await pool.acquire("account-a", "search")
    assert business_limited.value.reason == "rate_limit"
    with pytest.raises(PoolUnavailable) as control_limited:
        await pool.acquire("account-a", "auth/credits", control=True)
    assert control_limited.value.reason == "rate_limit"


@pytest.mark.asyncio
async def test_retry_after_is_not_truncated_by_local_cooldown_cap() -> None:
    clock = Clock()
    pool = KeyPool(
        make_settings(max_rate_limit_cooldown_seconds=300),
        monotonic=clock.monotonic,
        wall_time=clock.wall_time,
    )
    lease = await pool.acquire("account-a", "search")
    record = await pool.report_response(lease, 429, {"retry-after": "7200"})
    await lease.release()

    assert record is not None
    assert record.until_epoch == clock.wall + 7200
    with pytest.raises(PoolUnavailable) as captured:
        await pool.acquire("account-a", "search")
    assert captured.value.retry_after == 7200


@pytest.mark.asyncio
async def test_429_without_retry_after_uses_conservative_fallback() -> None:
    clock = Clock()
    pool = KeyPool(
        make_settings(max_rate_limit_cooldown_seconds=300),
        monotonic=clock.monotonic,
        wall_time=clock.wall_time,
    )
    lease = await pool.acquire("account-a", "search")

    record = await pool.report_response(lease, 429, {})
    await lease.release()

    assert record is not None
    assert record.until_epoch == clock.wall + 300


@pytest.mark.asyncio
async def test_403_opens_an_account_wide_circuit() -> None:
    clock = Clock()
    pool = KeyPool(
        make_settings(forbidden_cooldown_seconds=60),
        monotonic=clock.monotonic,
        wall_time=clock.wall_time,
    )
    lease = await pool.acquire("account-a", "search")
    record = await pool.report_response(lease, 403, {})
    await lease.release()

    assert record is not None
    assert (record.scope, record.name) == ("account", "forbidden")
    with pytest.raises(PoolUnavailable) as captured:
        await pool.acquire("account-a", "tools/execute")
    assert captured.value.reason == "forbidden"
    assert captured.value.retry_after == 60

    status = (await pool.status())[0]
    assert status["forbidden_cooldown"] == 60
    clock.advance(60)
    recovered = await pool.acquire("account-a", "tools/execute")
    await recovered.release()


@pytest.mark.asyncio
async def test_http_and_transport_failures_back_off_and_success_resets() -> None:
    clock = Clock()
    pool = KeyPool(
        make_settings(
            failure_backoff_base_seconds=2,
            failure_backoff_max_seconds=5,
        ),
        monotonic=clock.monotonic,
        wall_time=clock.wall_time,
        jitter=lambda: 1.0,
    )

    first = await pool.acquire("account-a", "search")
    first_record = await pool.report_response(first, 500, {})
    await first.release()
    assert first_record is not None
    assert first_record.failure_count == 1
    assert first_record.until_epoch == clock.wall + 2

    clock.advance(2)
    second = await pool.acquire("account-a", "search")
    second_record = await pool.report_transport_failure(second)
    await second.release()
    assert second_record.failure_count == 2
    assert second_record.until_epoch == clock.wall + 4

    clock.advance(4)
    third = await pool.acquire("account-a", "search")
    third_record = await pool.report_response(third, 503, {})
    await third.release()
    assert third_record is not None
    assert third_record.failure_count == 3
    assert third_record.until_epoch == clock.wall + 5

    clock.advance(5)
    recovery = await pool.acquire("account-a", "search")
    deletion = await pool.report_response(recovery, 200, {})
    await recovery.release()
    assert deletion is not None
    assert deletion.delete is True
    assert (deletion.scope, deletion.name) == ("health", "upstream")

    status = (await pool.status())[0]
    assert status["upstream_failure_count"] == 0
    assert status["upstream_cooldown"] == 0


@pytest.mark.asyncio
async def test_expired_failure_health_restores_the_exponential_sequence() -> None:
    clock = Clock()
    pool = KeyPool(
        make_settings(
            failure_backoff_base_seconds=2,
            failure_backoff_max_seconds=60,
        ),
        monotonic=clock.monotonic,
        wall_time=clock.wall_time,
        jitter=lambda: 1.0,
    )
    await pool.restore_cooldowns(
        [
            StoredCooldown(
                scope="health",
                account_id="account-a",
                name="upstream",
                until_epoch=clock.wall - 1,
                failure_count=2,
                retain_after_expiry=True,
            )
        ]
    )

    lease = await pool.acquire("account-a", "search")
    record = await pool.report_transport_failure(lease)
    await lease.release()
    assert record.failure_count == 3
    assert record.until_epoch == clock.wall + 8


@pytest.mark.asyncio
async def test_zero_credit_balance_persists_until_positive_probe() -> None:
    pool = KeyPool(make_settings())
    depleted = await pool.report_credit_balance("account-a", 0)
    assert depleted is not None
    assert depleted.retain_after_expiry is True

    with pytest.raises(PoolUnavailable) as captured:
        await pool.acquire("account-a", "search", credit_sensitive=True)
    assert captured.value.reason == "credits"

    probe = await pool.acquire("account-a", "auth/credits")
    await probe.release()
    recovered = await pool.report_credit_balance("account-a", 1)
    assert recovered is not None
    assert recovered.delete is True
    assert recovered.clears == (("account", "credits"),)

    lease = await pool.acquire("account-a", "search", credit_sensitive=True)
    await lease.release()
