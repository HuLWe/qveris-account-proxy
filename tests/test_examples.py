from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

from qveris_keeper.config import BrowserAccountConfig, KeeperSettings
from qveris_proxy.config import ProxySettings


ROOT = Path(__file__).resolve().parents[1]


def test_proxy_example_is_valid() -> None:
    payload = json.loads(
        (ROOT / "examples" / "accounts.example.json").read_text(encoding="utf-8")
    )

    settings = ProxySettings(
        accounts=payload["accounts"],
        proxy_access_token="fixture-proxy-access-token-0123456789",
    )

    assert [account.id for account in settings.accounts] == ["account-a", "account-b"]


def test_keeper_example_is_valid() -> None:
    payload = json.loads(
        (ROOT / "examples" / "keeper.example.json").read_text(encoding="utf-8")
    )
    host_payload = deepcopy(payload)
    for account in host_payload["accounts"]:
        posix_paths = [
            "profile_dir",
            "email_file",
            "password_file",
            "bootstrap_token_file",
        ]
        for name in posix_paths:
            if name not in account:
                continue
            assert account[name].startswith("/")
            account[name] = ROOT / "runtime" / "example" / account["id"] / name
        for name, value in account["proxy"].items():
            assert value.startswith("/")
            account["proxy"][name] = ROOT / "runtime" / "example" / account["id"] / name
    accounts = tuple(
        BrowserAccountConfig.model_validate(account)
        for account in host_payload["accounts"]
    )

    settings = KeeperSettings(
        accounts=accounts,
        admin_token="fixture-keeper-admin-token-0123456789",
        profile_root=ROOT / "runtime" / "example",
    )

    assert [account.id for account in settings.accounts] == ["account-a", "account-b"]
