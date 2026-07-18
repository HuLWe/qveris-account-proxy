from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError
from pydantic import field_validator, model_validator

QVERIS_BASE_URL = "https://qveris.ai/api/v1/"
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class ConfigurationError(RuntimeError):
    """A startup configuration error whose text never includes secret values."""


class APIKeyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    api_key: SecretStr = Field(repr=False)

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if not _IDENTIFIER.fullmatch(value):
            raise ValueError("key id must be a short URL-safe identifier")
        return value

    @field_validator("api_key")
    @classmethod
    def validate_api_key(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value().strip()) < 8:
            raise ValueError("API key is invalid")
        return SecretStr(value.get_secret_value().strip())


class OAuthTokenConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    access_token: SecretStr = Field(repr=False)

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if not _IDENTIFIER.fullmatch(value):
            raise ValueError("OAuth token id must be a short URL-safe identifier")
        return value

    @field_validator("access_token")
    @classmethod
    def validate_access_token(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value().strip()) < 8:
            raise ValueError("OAuth access token is invalid")
        return SecretStr(value.get_secret_value().strip())


class HTTPTransportConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    user_agent: str = Field(
        default="qveris-account-proxy/0.1.0",
        min_length=1,
        max_length=512,
    )
    accept_language: str = Field(
        default="en-US,en;q=0.9",
        min_length=1,
        max_length=256,
    )
    proxy_url_file: str | None = None

    @field_validator("user_agent", "accept_language")
    @classmethod
    def validate_header_value(cls, value: str) -> str:
        stripped = value.strip()
        try:
            stripped.encode("ascii")
        except UnicodeEncodeError:
            raise ValueError("HTTP profile header must be ASCII") from None
        if any(
            ord(character) < 0x20 or ord(character) == 0x7F for character in stripped
        ):
            raise ValueError("HTTP profile header contains control characters")
        return stripped

    @field_validator("proxy_url_file")
    @classmethod
    def validate_proxy_reference(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped or "\x00" in stripped:
            raise ValueError("proxy URL file reference is invalid")
        return stripped


class AccountConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    name: str | None = None
    weight: int = Field(default=1, ge=1, le=100)
    requests_per_minute: float = Field(default=10.0, ge=1, le=10_000)
    burst: int = Field(default=10, ge=1, le=10_000)
    transport: HTTPTransportConfig = Field(default_factory=HTTPTransportConfig)
    keys: tuple[APIKeyConfig, ...] = ()
    oauth_tokens: tuple[OAuthTokenConfig, ...] = ()

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if not _IDENTIFIER.fullmatch(value):
            raise ValueError("account id must be a short URL-safe identifier")
        return value

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized or len(normalized) > 64:
            raise ValueError("account name must contain between 1 and 64 characters")
        return normalized

    @model_validator(mode="after")
    def validate_credentials(self) -> AccountConfig:
        if not self.keys and not self.oauth_tokens:
            raise ValueError("account must contain at least one provider credential")
        key_ids = [key.id for key in self.keys]
        if len(key_ids) != len(set(key_ids)):
            raise ValueError("key ids must be unique within an account")
        token_ids = [token.id for token in self.oauth_tokens]
        if len(token_ids) != len(set(token_ids)):
            raise ValueError("OAuth token ids must be unique within an account")
        return self


class ProxySettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    accounts: tuple[AccountConfig, ...] = Field(min_length=1)
    proxy_access_token: SecretStr = Field(repr=False)
    default_account: str | None = None
    routing_mode: Literal["explicit", "round_robin"] = "round_robin"
    allow_oauth_route_fallback: bool = False
    admin_first_open_claim_enabled: bool = False
    state_path: str = "/data/state.db"
    affinity_ttl_seconds: float = Field(
        default=24 * 60 * 60, ge=60, le=30 * 24 * 60 * 60
    )
    affinity_capture_bytes: int = Field(
        default=2 * 1024 * 1024, ge=1024, le=16 * 1024 * 1024
    )
    quota_refresh_interval_seconds: float = Field(
        default=15 * 60, ge=0, le=24 * 60 * 60
    )
    accounts_reload_interval_seconds: float = Field(default=30.0, ge=0, le=3600)
    accounts_file_path: str | None = Field(default=None, exclude=True)
    config_write_enabled: bool = False
    max_request_body_bytes: int = Field(
        default=4 * 1024 * 1024, ge=1024, le=64 * 1024 * 1024
    )
    max_connections: int = Field(default=64, ge=1, le=1024)
    max_inflight_per_key: int = Field(default=8, ge=1, le=256)
    queue_timeout_seconds: float = Field(default=2.0, gt=0, le=30)
    connect_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    read_timeout_seconds: float = Field(default=120.0, gt=0, le=3600)
    write_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    pool_timeout_seconds: float = Field(default=5.0, gt=0, le=120)
    max_rate_limit_cooldown_seconds: float = Field(default=300.0, gt=0, le=3600)
    auth_failure_cooldown_seconds: float = Field(default=300.0, gt=0, le=3600)
    forbidden_cooldown_seconds: float = Field(default=3600.0, gt=0, le=24 * 60 * 60)
    failure_backoff_base_seconds: float = Field(default=2.0, gt=0, le=300)
    failure_backoff_max_seconds: float = Field(default=300.0, gt=0, le=3600)
    payment_required_cooldown_seconds: float = Field(
        default=3600.0, gt=0, le=24 * 60 * 60
    )

    @field_validator("proxy_access_token")
    @classmethod
    def validate_proxy_access_token(cls, value: SecretStr) -> SecretStr:
        token = value.get_secret_value().strip()
        if len(token) < 24:
            raise ValueError("proxy access token is too short")
        return SecretStr(token)

    @model_validator(mode="after")
    def validate_accounts(self) -> ProxySettings:
        if self.failure_backoff_max_seconds < self.failure_backoff_base_seconds:
            raise ValueError("failure backoff maximum must not be below its base")
        account_ids = [account.id for account in self.accounts]
        if len(account_ids) != len(set(account_ids)):
            raise ValueError("account ids must be unique")
        if self.default_account is not None and self.default_account not in account_ids:
            raise ValueError("default account does not exist")

        credential_values: list[str] = []
        for account in self.accounts:
            credential_values.extend(
                key.api_key.get_secret_value() for key in account.keys
            )
            credential_values.extend(
                token.access_token.get_secret_value() for token in account.oauth_tokens
            )
        if len(credential_values) != len(set(credential_values)):
            raise ValueError("provider credentials must be unique")
        if self.proxy_access_token.get_secret_value() in credential_values:
            raise ValueError("proxy token must differ from provider credentials")
        return self

    @property
    def effective_default_account(self) -> str | None:
        if self.default_account is not None:
            return self.default_account
        if len(self.accounts) == 1 or self.routing_mode == "round_robin":
            return self.accounts[0].id
        return None


def _read_text(path: Path, label: str) -> str:
    try:
        value = path.read_text(encoding="utf-8-sig").strip()
    except OSError:
        raise ConfigurationError(f"{label} file is unavailable") from None
    if not value:
        raise ConfigurationError(f"{label} file is empty")
    return value


def _accounts_from_document(document: str) -> Any:
    try:
        payload = json.loads(document)
    except json.JSONDecodeError:
        raise ConfigurationError("accounts configuration is not valid JSON") from None
    if not isinstance(payload, dict) or "accounts" not in payload:
        raise ConfigurationError("accounts configuration has an invalid structure")
    return payload["accounts"]


def parse_accounts_payload(payload: bytes) -> Any:
    try:
        document = payload.decode("utf-8-sig").strip()
    except UnicodeDecodeError:
        raise ConfigurationError("accounts configuration is not valid UTF-8") from None
    if not document:
        raise ConfigurationError("accounts configuration is empty")
    return _accounts_from_document(document)


def _read_accounts(path: Path) -> Any:
    return _accounts_from_document(_read_text(path, "accounts configuration"))


def _env_int(env: Mapping[str, str], name: str, default: int) -> int:
    try:
        return int(env.get(name, str(default)))
    except ValueError:
        raise ConfigurationError(f"{name} must be an integer") from None


def _env_float(env: Mapping[str, str], name: str, default: float) -> float:
    try:
        return float(env.get(name, str(default)))
    except ValueError:
        raise ConfigurationError(f"{name} must be a number") from None


def _env_bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} must be a boolean")


def load_settings(environ: Mapping[str, str] | None = None) -> ProxySettings:
    env = os.environ if environ is None else environ
    accounts_path = Path(
        env.get("QVP_ACCOUNTS_FILE", "/run/secrets/qveris_accounts.json")
    )
    token_path = Path(
        env.get("QVP_ACCESS_TOKEN_FILE", "/run/secrets/qveris_proxy_access_token")
    )

    raw: dict[str, Any] = {
        "accounts": _read_accounts(accounts_path),
        "proxy_access_token": _read_text(token_path, "proxy access token"),
        "default_account": env.get("QVP_DEFAULT_ACCOUNT") or None,
        "routing_mode": env.get("QVP_ROUTING_MODE", "round_robin"),
        "allow_oauth_route_fallback": _env_bool(
            env, "QVP_ALLOW_API_KEY_FOR_OAUTH_ROUTES", False
        ),
        "admin_first_open_claim_enabled": _env_bool(
            env, "QVP_ADMIN_FIRST_OPEN_CLAIM", False
        ),
        "state_path": env.get("QVP_STATE_PATH", "/data/state.db"),
        "affinity_ttl_seconds": _env_float(
            env, "QVP_AFFINITY_TTL_SECONDS", 24 * 60 * 60
        ),
        "affinity_capture_bytes": _env_int(
            env, "QVP_AFFINITY_CAPTURE_BYTES", 2 * 1024 * 1024
        ),
        "quota_refresh_interval_seconds": _env_float(
            env, "QVP_QUOTA_REFRESH_INTERVAL_SECONDS", 15 * 60
        ),
        "accounts_reload_interval_seconds": _env_float(
            env, "QVP_ACCOUNTS_RELOAD_INTERVAL_SECONDS", 30.0
        ),
        "accounts_file_path": str(accounts_path),
        "config_write_enabled": _env_bool(env, "QVP_CONFIG_WRITE_ENABLED", False),
        "max_request_body_bytes": _env_int(
            env, "QVP_MAX_REQUEST_BODY_BYTES", 4 * 1024 * 1024
        ),
        "max_connections": _env_int(env, "QVP_MAX_CONNECTIONS", 64),
        "max_inflight_per_key": _env_int(env, "QVP_MAX_INFLIGHT_PER_KEY", 8),
        "queue_timeout_seconds": _env_float(env, "QVP_QUEUE_TIMEOUT_SECONDS", 2.0),
        "connect_timeout_seconds": _env_float(env, "QVP_CONNECT_TIMEOUT_SECONDS", 10.0),
        "read_timeout_seconds": _env_float(env, "QVP_READ_TIMEOUT_SECONDS", 120.0),
        "write_timeout_seconds": _env_float(env, "QVP_WRITE_TIMEOUT_SECONDS", 30.0),
        "pool_timeout_seconds": _env_float(env, "QVP_POOL_TIMEOUT_SECONDS", 5.0),
        "max_rate_limit_cooldown_seconds": _env_float(
            env, "QVP_MAX_RATE_LIMIT_COOLDOWN_SECONDS", 300.0
        ),
        "auth_failure_cooldown_seconds": _env_float(
            env, "QVP_AUTH_FAILURE_COOLDOWN_SECONDS", 300.0
        ),
        "forbidden_cooldown_seconds": _env_float(
            env, "QVP_FORBIDDEN_COOLDOWN_SECONDS", 3600.0
        ),
        "failure_backoff_base_seconds": _env_float(
            env, "QVP_FAILURE_BACKOFF_BASE_SECONDS", 2.0
        ),
        "failure_backoff_max_seconds": _env_float(
            env, "QVP_FAILURE_BACKOFF_MAX_SECONDS", 300.0
        ),
        "payment_required_cooldown_seconds": _env_float(
            env, "QVP_PAYMENT_REQUIRED_COOLDOWN_SECONDS", 3600.0
        ),
    }
    try:
        return ProxySettings.model_validate(raw)
    except ValidationError:
        raise ConfigurationError("proxy configuration is invalid") from None
