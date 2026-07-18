from __future__ import annotations

import json
from datetime import time, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from qveris_keeper.config import (
    BrowserAccountConfig,
    ConfigurationError,
    KeeperSettings,
    load_settings,
    read_proxy_options,
)
from test_keeper_helpers import ADMIN_TOKEN, make_account, write_secret


def test_load_settings_reads_only_external_secret_references(tmp_path: Path) -> None:
    profile_root = tmp_path / "profiles"
    secret_dir = tmp_path / "secrets"
    secret_dir.mkdir()
    files = {
        "email": write_secret(secret_dir / "email", "user@example.test"),
        "password": write_secret(secret_dir / "password", "password-sentinel"),
        "proxy": write_secret(secret_dir / "proxy", "http://proxy.test:8080"),
        "proxy_user": write_secret(secret_dir / "proxy-user", "proxy-user"),
        "proxy_password": write_secret(secret_dir / "proxy-password", "proxy-password"),
        "admin": write_secret(secret_dir / "admin", ADMIN_TOKEN),
    }
    config = {
        "accounts": [
            {
                "id": "account-a",
                "login_mode": "email",
                "profile_dir": str(profile_root / "account-a"),
                "email_file": str(files["email"]),
                "password_file": str(files["password"]),
                "proxy": {
                    "server_file": str(files["proxy"]),
                    "username_file": str(files["proxy_user"]),
                    "password_file": str(files["proxy_password"]),
                },
                "locale": "en-US",
                "timezone_id": "UTC",
                "viewport": {"width": 1365, "height": 768},
                "user_agent": "Mozilla/5.0 fixture Chrome/138.0.0.0 Safari/537.36",
                "daily_touch_time": "06:30:00",
            }
        ]
    }
    config_path = tmp_path / "keeper.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    settings = load_settings(
        {
            "QVK_CONFIG_FILE": str(config_path),
            "QVK_ADMIN_TOKEN_FILE": str(files["admin"]),
            "QVK_PROFILE_ROOT": str(profile_root),
            "QVK_STATE_PATH": str(tmp_path / "runtime" / "keeper.db"),
        }
    )

    assert settings.accounts[0].email_file == files["email"]
    assert settings.accounts[0].password_file == files["password"]
    assert settings.admin_token.get_secret_value() == ADMIN_TOKEN
    assert ADMIN_TOKEN not in repr(settings)


@pytest.mark.parametrize("field", ["password", "token", "proxy_password"])
def test_inline_credentials_are_rejected(tmp_path: Path, field: str) -> None:
    account = make_account(tmp_path).model_dump(mode="json")
    account[field] = "inline-secret-sentinel"
    with pytest.raises(ValidationError):
        BrowserAccountConfig.model_validate(account)


def test_manual_account_rejects_email_secret_fields(tmp_path: Path) -> None:
    account = make_account(tmp_path).model_dump()
    account["login_mode"] = "manual"
    with pytest.raises(ValidationError):
        BrowserAccountConfig.model_validate(account)


def test_profiles_must_be_distinct_and_below_root(tmp_path: Path) -> None:
    first = make_account(tmp_path, account_id="account-a")
    second = make_account(tmp_path, account_id="account-b").model_copy(
        update={"profile_dir": first.profile_dir}
    )
    with pytest.raises(ValidationError):
        KeeperSettings(
            accounts=(first, second),
            admin_token=ADMIN_TOKEN,
            profile_root=tmp_path / "profiles",
        )


def test_proxy_authentication_in_server_url_is_rejected(tmp_path: Path) -> None:
    account = make_account(tmp_path)
    account.proxy.server_file.write_text(
        "http://user:password@proxy.test:8080", encoding="utf-8"
    )
    with pytest.raises(ConfigurationError, match="separate files"):
        read_proxy_options(account.proxy)


def test_configuration_errors_do_not_echo_invalid_json(tmp_path: Path) -> None:
    config_path = tmp_path / "keeper.json"
    config_path.write_text('{"password":"secret-sentinel"', encoding="utf-8")
    admin_path = write_secret(tmp_path / "admin", ADMIN_TOKEN)
    with pytest.raises(ConfigurationError) as caught:
        load_settings(
            {
                "QVK_CONFIG_FILE": str(config_path),
                "QVK_ADMIN_TOKEN_FILE": str(admin_path),
            }
        )
    assert "secret-sentinel" not in str(caught.value)


def test_inline_admin_token_is_rejected(tmp_path: Path) -> None:
    config_path = tmp_path / "keeper.json"
    config_path.write_text(
        json.dumps({"accounts": [], "admin_token": "inline-secret-sentinel"}),
        encoding="utf-8",
    )
    admin_path = write_secret(tmp_path / "admin", ADMIN_TOKEN)

    with pytest.raises(ConfigurationError, match="external token file"):
        load_settings(
            {
                "QVK_CONFIG_FILE": str(config_path),
                "QVK_ADMIN_TOKEN_FILE": str(admin_path),
            }
        )


def test_daily_touch_time_uses_account_local_wall_clock(tmp_path: Path) -> None:
    account = make_account(tmp_path).model_dump()
    account["daily_touch_time"] = time(6, tzinfo=timezone.utc)

    with pytest.raises(ValidationError, match="local wall time"):
        BrowserAccountConfig.model_validate(account)


def test_iana_timezone_is_validated_with_bundled_tzdata(tmp_path: Path) -> None:
    account = make_account(tmp_path).model_dump()
    account["timezone_id"] = "Asia/Shanghai"

    validated = BrowserAccountConfig.model_validate(account)

    assert validated.timezone_id == "Asia/Shanghai"
