from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from .config import (
    APIKeyConfig,
    AccountConfig,
    ConfigurationError,
    HTTPTransportConfig,
    OAuthTokenConfig,
    ProxySettings,
)

ADMIN_CONFIG_MAX_BYTES = 256 * 1024


class AdminConfigError(RuntimeError):
    """A stable, secret-free admin configuration failure."""


class CredentialInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    value: SecretStr | None = Field(default=None, repr=False)


class TransportInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_agent: str = "qveris-account-proxy/0.1.0"
    accept_language: str = "en-US,en;q=0.9"
    proxy_url_file: str | None = None


class AccountInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str | None = None
    weight: int = 1
    requests_per_minute: float = 10.0
    burst: int = 10
    transport: TransportInput = Field(default_factory=TransportInput)
    keys: tuple[CredentialInput, ...] = ()
    oauth_tokens: tuple[CredentialInput, ...] = ()


class AccountsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    accounts: tuple[AccountInput, ...] = Field(min_length=1)


def public_config(settings: ProxySettings) -> dict[str, object]:
    return {
        "schema_version": 1,
        "capabilities": {
            "persistent_editing": (
                settings.config_write_enabled
                and settings.accounts_file_path is not None
            ),
            "validation": True,
            "account_test": True,
            "api_console": True,
        },
        "routing": {
            "mode": settings.routing_mode,
            "default_account": settings.effective_default_account,
            "configured_default_account": settings.default_account,
        },
        "accounts": [
            {
                "id": account.id,
                "name": account.name or account.id,
                "weight": account.weight,
                "requests_per_minute": account.requests_per_minute,
                "burst": account.burst,
                "transport": {
                    "user_agent": account.transport.user_agent,
                    "accept_language": account.transport.accept_language,
                    "proxy_configured": account.transport.proxy_url_file is not None,
                },
                "keys": [
                    {"id": credential.id, "configured": True}
                    for credential in account.keys
                ],
                "oauth_tokens": [
                    {"id": credential.id, "configured": True}
                    for credential in account.oauth_tokens
                ],
            }
            for account in settings.accounts
        ],
    }


def parse_admin_accounts(
    payload: bytes,
    current_accounts: tuple[AccountConfig, ...],
) -> tuple[AccountConfig, ...]:
    if len(payload) > ADMIN_CONFIG_MAX_BYTES:
        raise AdminConfigError("config_too_large")
    try:
        document = json.loads(payload.decode("utf-8-sig"))
        submitted = AccountsInput.model_validate(document)
    except (UnicodeDecodeError, json.JSONDecodeError, ValidationError):
        raise AdminConfigError("invalid_config") from None

    current_by_id = {account.id: account for account in current_accounts}
    accounts: list[AccountConfig] = []
    try:
        for item in submitted.accounts:
            previous = current_by_id.get(item.id)
            previous_keys = (
                {credential.id: credential for credential in previous.keys}
                if previous is not None
                else {}
            )
            previous_tokens = (
                {credential.id: credential for credential in previous.oauth_tokens}
                if previous is not None
                else {}
            )
            keys = tuple(
                APIKeyConfig(
                    id=credential.id,
                    api_key=_credential_value(
                        credential,
                        previous_keys.get(credential.id),
                        kind="api_key",
                    ),
                )
                for credential in item.keys
            )
            oauth_tokens = tuple(
                OAuthTokenConfig(
                    id=credential.id,
                    access_token=_credential_value(
                        credential,
                        previous_tokens.get(credential.id),
                        kind="oauth",
                    ),
                )
                for credential in item.oauth_tokens
            )
            proxy_url_file = item.transport.proxy_url_file
            if proxy_url_file is None and previous is not None:
                proxy_url_file = previous.transport.proxy_url_file
            account_name = item.name
            if account_name is None:
                previous_name = previous.name if previous is not None else None
                account_name = previous_name or item.id
            accounts.append(
                AccountConfig(
                    id=item.id,
                    name=account_name,
                    weight=item.weight,
                    requests_per_minute=item.requests_per_minute,
                    burst=item.burst,
                    transport=HTTPTransportConfig(
                        user_agent=item.transport.user_agent,
                        accept_language=item.transport.accept_language,
                        proxy_url_file=proxy_url_file,
                    ),
                    keys=keys,
                    oauth_tokens=oauth_tokens,
                )
            )
    except (ValidationError, ConfigurationError):
        raise AdminConfigError("invalid_config") from None
    return tuple(accounts)


def _credential_value(
    submitted: CredentialInput,
    previous: APIKeyConfig | OAuthTokenConfig | None,
    *,
    kind: str,
) -> SecretStr:
    if submitted.value is not None:
        return submitted.value
    if isinstance(previous, APIKeyConfig):
        return previous.api_key
    if isinstance(previous, OAuthTokenConfig):
        return previous.access_token
    raise AdminConfigError(f"missing_{kind}_value")


def serialize_accounts(accounts: tuple[AccountConfig, ...]) -> bytes:
    document: dict[str, Any] = {"accounts": []}
    rendered_accounts: list[dict[str, Any]] = document["accounts"]
    for account in accounts:
        rendered_accounts.append(
            {
                "id": account.id,
                "name": account.name or account.id,
                "weight": account.weight,
                "requests_per_minute": account.requests_per_minute,
                "burst": account.burst,
                "transport": {
                    "user_agent": account.transport.user_agent,
                    "accept_language": account.transport.accept_language,
                    **(
                        {"proxy_url_file": account.transport.proxy_url_file}
                        if account.transport.proxy_url_file is not None
                        else {}
                    ),
                },
                "keys": [
                    {
                        "id": credential.id,
                        "api_key": credential.api_key.get_secret_value(),
                    }
                    for credential in account.keys
                ],
                "oauth_tokens": [
                    {
                        "id": credential.id,
                        "access_token": credential.access_token.get_secret_value(),
                    }
                    for credential in account.oauth_tokens
                ],
            }
        )
    return (
        json.dumps(document, ensure_ascii=True, indent=2, separators=(",", ": ")) + "\n"
    ).encode("utf-8")


def write_accounts_atomic(path: str | Path, payload: bytes) -> None:
    target = Path(path)
    directory = target.parent
    file_descriptor: int | None = None
    temporary_path: str | None = None
    try:
        file_descriptor, temporary_path = tempfile.mkstemp(
            prefix=".accounts-",
            suffix=".tmp",
            dir=directory,
        )
        if hasattr(os, "fchmod"):
            os.fchmod(file_descriptor, 0o600)
        with os.fdopen(file_descriptor, "wb") as handle:
            file_descriptor = None
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, target)
        temporary_path = None
    except OSError:
        raise AdminConfigError("write_failed") from None
    finally:
        if file_descriptor is not None:
            os.close(file_descriptor)
        if temporary_path is not None:
            try:
                os.unlink(temporary_path)
            except OSError:
                pass
