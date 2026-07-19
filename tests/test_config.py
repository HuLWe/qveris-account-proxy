from __future__ import annotations

import pytest
from pydantic import ValidationError

from qveris_proxy.config import (
    AccountConfig,
    APIKeyConfig,
    HTTPTransportConfig,
    OAuthTokenConfig,
    ProxySettings,
)
from conftest import ACCESS_TOKEN, KEY_A1, OAUTH_A1, make_settings


def test_settings_repr_redacts_all_secrets() -> None:
    settings = make_settings()

    rendered = repr(settings)

    assert ACCESS_TOKEN not in rendered
    assert KEY_A1 not in rendered
    assert OAUTH_A1 not in rendered
    assert "api_key" not in rendered
    assert "access_token" not in rendered
    assert "proxy_access_token" not in rendered


def test_account_display_name_is_trimmed_and_validated() -> None:
    account = AccountConfig(
        id="account-a",
        name="  主账号  ",
        keys=(APIKeyConfig(id="primary", api_key=KEY_A1),),
    )
    assert account.name == "主账号"

    with pytest.raises(ValidationError):
        AccountConfig(
            id="account-a",
            name="   ",
            keys=(APIKeyConfig(id="primary", api_key=KEY_A1),),
        )

    with pytest.raises(ValidationError):
        AccountConfig(
            id="account-a",
            name="x" * 65,
            keys=(APIKeyConfig(id="primary", api_key=KEY_A1),),
        )


def test_rejects_duplicate_provider_keys_without_echoing_value() -> None:
    with pytest.raises(ValidationError) as captured:
        ProxySettings(
            proxy_access_token=ACCESS_TOKEN,
            accounts=(
                AccountConfig(
                    id="account-a",
                    keys=(
                        APIKeyConfig(id="one", api_key=KEY_A1),
                        APIKeyConfig(id="two", api_key=KEY_A1),
                    ),
                ),
            ),
        )

    assert KEY_A1 not in str(captured.value)


def test_proxy_token_must_differ_from_provider_key() -> None:
    with pytest.raises(ValidationError):
        ProxySettings(
            proxy_access_token=KEY_A1,
            accounts=(
                AccountConfig(
                    id="account-a",
                    keys=(APIKeyConfig(id="one", api_key=KEY_A1),),
                ),
            ),
        )


def test_account_can_hold_only_an_oauth_token() -> None:
    settings = ProxySettings(
        proxy_access_token=ACCESS_TOKEN,
        accounts=(
            AccountConfig(
                id="audit-only",
                oauth_tokens=(OAuthTokenConfig(id="primary", access_token=OAUTH_A1),),
            ),
        ),
    )

    assert settings.accounts[0].keys == ()
    assert len(settings.accounts[0].oauth_tokens) == 1


def test_multiple_accounts_use_a_dynamic_default_only_for_round_robin() -> None:
    settings = make_settings(multiple_accounts=True)
    assert settings.effective_default_account is None

    round_robin = make_settings(multiple_accounts=True, routing_mode="round_robin")
    assert round_robin.effective_default_account == "account-a"

    settings_with_default = make_settings(
        multiple_accounts=True, default_account="account-b"
    )
    assert settings_with_default.effective_default_account == "account-b"


def test_empty_accounts_are_a_valid_waiting_state() -> None:
    settings = ProxySettings(
        proxy_access_token=ACCESS_TOKEN,
        accounts=(),
    )

    assert settings.accounts == ()
    assert settings.effective_default_account is None

    with pytest.raises(ValidationError):
        ProxySettings(
            proxy_access_token=ACCESS_TOKEN,
            accounts=(),
            default_account="removed-account",
        )


def test_first_open_browser_claim_is_opt_in() -> None:
    assert make_settings().admin_first_open_claim_enabled is False
    assert (
        make_settings(
            admin_first_open_claim_enabled=True
        ).admin_first_open_claim_enabled
        is True
    )


def test_account_rate_limit_defaults_are_conservative_and_configurable() -> None:
    default_account = AccountConfig(
        id="default-rate",
        keys=(APIKeyConfig(id="primary", api_key=KEY_A1),),
    )
    assert default_account.requests_per_minute == 10
    assert default_account.burst == 10

    configured = AccountConfig(
        id="rate-limited",
        requests_per_minute=60,
        burst=3,
        keys=(APIKeyConfig(id="primary", api_key=KEY_A1),),
    )
    assert configured.requests_per_minute == 60
    assert configured.burst == 3


def test_failure_backoff_maximum_must_cover_base() -> None:
    with pytest.raises(ValidationError):
        make_settings(
            failure_backoff_base_seconds=30,
            failure_backoff_max_seconds=10,
        )


def test_account_transport_profile_is_stable_and_file_referenced() -> None:
    account = AccountConfig(
        id="account-a",
        transport=HTTPTransportConfig(
            user_agent="fixture-agent/1.0",
            accept_language="zh-CN,zh;q=0.9",
            proxy_url_file="/run/account-secrets/account-a-proxy-url",
        ),
        keys=(APIKeyConfig(id="primary", api_key=KEY_A1),),
    )

    assert account.transport.user_agent == "fixture-agent/1.0"
    assert account.transport.accept_language == "zh-CN,zh;q=0.9"
    assert account.transport.proxy_url_file == (
        "/run/account-secrets/account-a-proxy-url"
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("user_agent", "fixture\r\ninjected: value"),
        ("accept_language", "zh-CN\x00invalid"),
    ],
)
def test_transport_profile_rejects_header_injection(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        HTTPTransportConfig(**{field: value})
