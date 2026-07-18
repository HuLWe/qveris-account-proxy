from __future__ import annotations

from qveris_proxy.config import (
    AccountConfig,
    APIKeyConfig,
    OAuthTokenConfig,
    ProxySettings,
)

ACCESS_TOKEN = "sentinel-proxy-access-token-0123456789"
KEY_A1 = "sentinel-provider-key-account-a-primary"
KEY_A2 = "sentinel-provider-key-account-a-standby"
KEY_B1 = "sentinel-provider-key-account-b-primary"
OAUTH_A1 = "sentinel-oauth-token-account-a-primary"
OAUTH_B1 = "sentinel-oauth-token-account-b-primary"


def make_settings(*, multiple_accounts: bool = False, **overrides) -> ProxySettings:
    accounts = [
        AccountConfig(
            id="account-a",
            requests_per_minute=10_000,
            burst=10_000,
            keys=(
                APIKeyConfig(id="primary", api_key=KEY_A1),
                APIKeyConfig(id="standby", api_key=KEY_A2),
            ),
            oauth_tokens=(OAuthTokenConfig(id="primary", access_token=OAUTH_A1),),
        )
    ]
    if multiple_accounts:
        accounts.append(
            AccountConfig(
                id="account-b",
                requests_per_minute=10_000,
                burst=10_000,
                keys=(APIKeyConfig(id="primary", api_key=KEY_B1),),
                oauth_tokens=(OAuthTokenConfig(id="primary", access_token=OAUTH_B1),),
            )
        )
    values = {
        "accounts": tuple(accounts),
        "proxy_access_token": ACCESS_TOKEN,
        "queue_timeout_seconds": 0.25,
        "routing_mode": "explicit",
        "state_path": ":memory:",
        "quota_refresh_interval_seconds": 0,
    }
    values.update(overrides)
    return ProxySettings(**values)
