from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time, timezone
from pathlib import Path

import pytest

from qveris_keeper.browser import SessionObservation
from qveris_keeper.service import KeeperService
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


@pytest.mark.asyncio
async def test_email_account_logs_in_after_missing_session(tmp_path: Path) -> None:
    account = make_account(tmp_path)
    browser = FakeAccountBrowser(
        probes=[SessionObservation("unauthenticated", 401)],
        logins=[SessionObservation("authenticated", 200, 200)],
    )
    runtime = FakeBrowserRuntime({account.id: browser})
    service = KeeperService(
        make_settings(tmp_path, (account,)),
        runtime=runtime,
        wall_time=MutableClock(NOW),
    )

    await service.start()
    status = service.account_status()[0]

    assert status["state"] == "authenticated"
    assert status["ready"] is True
    assert browser.login_calls == 1
    assert browser.received_email == EMAIL_VALUE
    assert browser.received_password == PASSWORD_VALUE
    await service.close()
    assert browser.closed is True
    assert runtime.closed is True


@pytest.mark.asyncio
async def test_manual_profile_reports_manual_action_required(tmp_path: Path) -> None:
    account = make_account(tmp_path, login_mode="manual")
    browser = FakeAccountBrowser(probes=[SessionObservation("unauthenticated", 401)])
    service = KeeperService(
        make_settings(tmp_path, (account,)),
        runtime=FakeBrowserRuntime({account.id: browser}),
        wall_time=MutableClock(NOW),
    )

    await service.start()

    assert service.account_status()[0]["state"] == "manual_action_required"
    assert browser.login_calls == 0
    await service.close()


@pytest.mark.asyncio
async def test_bootstrap_token_is_applied_inside_browser(tmp_path: Path) -> None:
    account = make_account(tmp_path, login_mode="manual", bootstrap_token=True)
    browser = FakeAccountBrowser(
        probes=[
            SessionObservation("unauthenticated", 401),
            SessionObservation("authenticated", 200, 200),
        ]
    )
    service = KeeperService(
        make_settings(tmp_path, (account,)),
        runtime=FakeBrowserRuntime({account.id: browser}),
        wall_time=MutableClock(NOW),
    )

    await service.start()

    assert service.account_status()[0]["state"] == "authenticated"
    assert browser.bootstrap_calls == 1
    assert browser.received_token == BOOTSTRAP_TOKEN
    await service.close()


@pytest.mark.asyncio
async def test_challenge_transitions_to_explicit_state(tmp_path: Path) -> None:
    account = make_account(tmp_path)
    browser = FakeAccountBrowser(probes=[SessionObservation("challenge", 403)])
    service = KeeperService(
        make_settings(tmp_path, (account,)),
        runtime=FakeBrowserRuntime({account.id: browser}),
        wall_time=MutableClock(NOW),
    )

    await service.start()
    status = service.account_status()[0]

    assert status["state"] == "challenge_required"
    assert status["reason"] == "challenge_detected"
    assert browser.login_calls == 0
    await service.close()


@pytest.mark.asyncio
async def test_daily_email_login_and_session_touch(tmp_path: Path) -> None:
    account = make_account(tmp_path, daily_touch_time=time(23, 59))
    browser = FakeAccountBrowser(
        probes=[SessionObservation("authenticated", 200, 200)],
        logins=[SessionObservation("authenticated", 200, 200)],
        touches=[SessionObservation("authenticated", 200, 200)],
    )
    clock = MutableClock(NOW)
    service = KeeperService(
        make_settings(tmp_path, (account,)),
        runtime=FakeBrowserRuntime({account.id: browser}),
        wall_time=clock,
    )
    await service.start()

    clock.value = datetime(2026, 1, 2, 23, 59, tzinfo=timezone.utc).timestamp()
    await service.run_due(clock.value)
    status = service.account_status()[0]

    assert browser.login_calls == 1
    assert browser.touch_calls == 1
    assert status["state"] == "authenticated"
    assert status["last_login_at"] is not None
    assert status["last_touch_at"] is not None
    await service.close()


@pytest.mark.asyncio
async def test_daily_checkin_can_be_disabled_per_account(tmp_path: Path) -> None:
    account = make_account(tmp_path, daily_touch_time=time(6)).model_copy(
        update={"daily_checkin_enabled": False}
    )
    browser = FakeAccountBrowser(
        probes=[SessionObservation("authenticated", 200, 200)]
    )
    clock = MutableClock(NOW)
    service = KeeperService(
        make_settings(tmp_path, (account,)),
        runtime=FakeBrowserRuntime({account.id: browser}),
        wall_time=clock,
    )
    await service.start()

    clock.value = datetime(2026, 1, 2, 6, tzinfo=timezone.utc).timestamp()
    await service.run_due(clock.value)

    assert browser.login_calls == 0
    assert browser.touch_calls == 0
    await service.close()


@pytest.mark.asyncio
async def test_daily_checkin_marks_successful_login(tmp_path: Path) -> None:
    account = make_account(tmp_path, daily_touch_time=time(23, 59))
    browser = FakeAccountBrowser(
        probes=[SessionObservation("authenticated", 200, 200)],
        touches=[SessionObservation("authenticated", 200, 200)],
    )
    clock = MutableClock(NOW)
    service = KeeperService(
        make_settings(tmp_path, (account,)),
        runtime=FakeBrowserRuntime({account.id: browser}),
        wall_time=clock,
    )
    await service.start()

    clock.value = datetime(2026, 1, 2, 23, 59, tzinfo=timezone.utc).timestamp()
    await service.run_due(clock.value)

    assert service.account_status()[0]["reason"] == "daily_checkin_claimed"
    await service.close()


@pytest.mark.asyncio
async def test_startup_login_is_not_repeated_by_daily_scheduler(
    tmp_path: Path,
) -> None:
    account = make_account(tmp_path, daily_touch_time=time(6))
    browser = FakeAccountBrowser(
        probes=[SessionObservation("unauthenticated", 401)],
        logins=[SessionObservation("authenticated", 200, 200)],
    )
    clock = MutableClock(NOW)
    service = KeeperService(
        make_settings(tmp_path, (account,)),
        runtime=FakeBrowserRuntime({account.id: browser}),
        wall_time=clock,
    )

    await service.start()
    await asyncio.sleep(0)

    assert browser.login_calls == 1
    assert service.account_status()[0]["last_touch_at"] is not None
    await service.close()


@pytest.mark.asyncio
async def test_persisted_touch_date_prevents_duplicate_daily_login(
    tmp_path: Path,
) -> None:
    account = make_account(tmp_path, daily_touch_time=time(23, 59))
    clock = MutableClock(NOW)
    first_browser = FakeAccountBrowser(
        probes=[SessionObservation("authenticated", 200, 200)]
    )
    settings = make_settings(tmp_path, (account,))
    first = KeeperService(
        settings,
        runtime=FakeBrowserRuntime({account.id: first_browser}),
        wall_time=clock,
    )
    await first.start()
    clock.value = datetime(2026, 1, 2, 23, 59, tzinfo=timezone.utc).timestamp()
    await first.run_due(clock.value)
    assert first_browser.login_calls == 1
    await first.close()

    second_browser = FakeAccountBrowser(
        probes=[SessionObservation("authenticated", 200, 200)]
    )
    second = KeeperService(
        settings,
        runtime=FakeBrowserRuntime({account.id: second_browser}),
        wall_time=clock,
    )
    await second.start()
    await asyncio.sleep(0)

    assert second_browser.login_calls == 0
    assert second_browser.touch_calls == 0
    await second.close()


@pytest.mark.asyncio
async def test_account_actions_are_serialized(tmp_path: Path) -> None:
    account = make_account(tmp_path)
    browser = FakeAccountBrowser(
        probes=[SessionObservation("authenticated", 200, 200)], delay=0.02
    )
    service = KeeperService(
        make_settings(tmp_path, (account,)),
        runtime=FakeBrowserRuntime({account.id: browser}),
        wall_time=MutableClock(NOW),
    )
    await service.start()

    await asyncio.gather(
        service.refresh_account(account.id), service.refresh_account(account.id)
    )

    assert browser.max_active == 1
    await service.close()


@pytest.mark.asyncio
async def test_missing_secret_is_sanitized_state(tmp_path: Path) -> None:
    account = make_account(tmp_path)
    assert account.password_file is not None
    account.password_file.unlink()
    browser = FakeAccountBrowser(probes=[SessionObservation("unauthenticated", 401)])
    service = KeeperService(
        make_settings(tmp_path, (account,)),
        runtime=FakeBrowserRuntime({account.id: browser}),
        wall_time=MutableClock(NOW),
    )

    await service.start()
    status = service.account_status()[0]

    assert status["state"] == "manual_action_required"
    assert status["reason"] == "secret_unavailable"
    await service.close()


@pytest.mark.asyncio
async def test_sqlite_contains_only_non_sensitive_status(tmp_path: Path) -> None:
    account = make_account(tmp_path, bootstrap_token=True)
    browser = FakeAccountBrowser(probes=[SessionObservation("authenticated", 200, 200)])
    settings = make_settings(tmp_path, (account,))
    service = KeeperService(
        settings,
        runtime=FakeBrowserRuntime({account.id: browser}),
        wall_time=MutableClock(NOW),
    )
    await service.start()
    await service.close()

    database = Path(settings.state_path).read_bytes()
    for secret in (
        ADMIN_TOKEN,
        EMAIL_VALUE,
        PASSWORD_VALUE,
        BOOTSTRAP_TOKEN,
        PROXY_USERNAME,
        PROXY_PASSWORD,
    ):
        assert secret.encode() not in database


@pytest.mark.asyncio
async def test_runtime_close_failure_still_closes_state_store(tmp_path: Path) -> None:
    account = make_account(tmp_path)
    browser = FakeAccountBrowser(probes=[SessionObservation("authenticated", 200, 200)])

    class RaisingCloseRuntime(FakeBrowserRuntime):
        async def close(self) -> None:
            raise RuntimeError("close fixture")

    service = KeeperService(
        make_settings(tmp_path, (account,)),
        runtime=RaisingCloseRuntime({account.id: browser}),
        wall_time=MutableClock(NOW),
    )
    await service.start()

    await service.close()

    assert browser.closed is True


@pytest.mark.asyncio
async def test_browser_exception_messages_are_redacted_from_logs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    account = make_account(tmp_path)

    class SecretErrorBrowser(FakeAccountBrowser):
        async def probe(self) -> SessionObservation:
            raise RuntimeError(f"transport included {PASSWORD_VALUE}")

    browser = SecretErrorBrowser()
    service = KeeperService(
        make_settings(tmp_path, (account,)),
        runtime=FakeBrowserRuntime({account.id: browser}),
        wall_time=MutableClock(NOW),
    )

    with caplog.at_level(logging.WARNING, logger="qveris_keeper.service"):
        await service.start()

    assert service.account_status()[0]["reason"] == "browser_error"
    assert PASSWORD_VALUE not in caplog.text
    assert "RuntimeError" in caplog.text
    await service.close()
