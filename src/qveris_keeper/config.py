from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from datetime import time, timezone, tzinfo
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError
from pydantic import field_validator, model_validator

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_LOCALE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")


class ConfigurationError(RuntimeError):
    """A configuration error whose message excludes secret values."""


def resolve_timezone(value: str) -> tzinfo:
    if value in {"UTC", "Etc/UTC"}:
        return timezone.utc
    return ZoneInfo(value)


def _absolute_path(value: Path) -> Path:
    if not value.is_absolute():
        raise ValueError("path must be absolute")
    return value


class ViewportConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    width: int = Field(ge=640, le=7680)
    height: int = Field(ge=480, le=4320)
    device_scale_factor: float = Field(default=1.0, ge=0.5, le=4.0)


class ProxyFileConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    server_file: Path
    username_file: Path | None = None
    password_file: Path | None = None

    @field_validator("server_file", "username_file", "password_file")
    @classmethod
    def validate_paths(cls, value: Path | None) -> Path | None:
        return None if value is None else _absolute_path(value)

    @model_validator(mode="after")
    def validate_auth_files(self) -> ProxyFileConfig:
        if (self.username_file is None) != (self.password_file is None):
            raise ValueError("proxy username and password files must be paired")
        return self


class BrowserAccountConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    login_mode: Literal["email", "manual"]
    profile_dir: Path
    email_file: Path | None = None
    password_file: Path | None = None
    bootstrap_token_file: Path | None = None
    proxy: ProxyFileConfig
    locale: str
    timezone_id: str
    viewport: ViewportConfig
    user_agent: str = Field(min_length=20, max_length=512)
    headless: bool = True
    daily_touch_time: time = time(hour=6)

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if not _IDENTIFIER.fullmatch(value):
            raise ValueError("account id must be a short URL-safe identifier")
        return value

    @field_validator(
        "profile_dir", "email_file", "password_file", "bootstrap_token_file"
    )
    @classmethod
    def validate_paths(cls, value: Path | None) -> Path | None:
        return None if value is None else _absolute_path(value)

    @field_validator("locale")
    @classmethod
    def validate_locale(cls, value: str) -> str:
        if not _LOCALE.fullmatch(value):
            raise ValueError("locale is invalid")
        return value

    @field_validator("timezone_id")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            resolve_timezone(value)
        except ZoneInfoNotFoundError:
            raise ValueError("timezone is invalid") from None
        return value

    @field_validator("user_agent")
    @classmethod
    def validate_user_agent(cls, value: str) -> str:
        if "\r" in value or "\n" in value:
            raise ValueError("user agent is invalid")
        return value

    @field_validator("daily_touch_time")
    @classmethod
    def validate_daily_touch_time(cls, value: time) -> time:
        if value.tzinfo is not None:
            raise ValueError("daily touch time must be local wall time")
        return value

    @model_validator(mode="after")
    def validate_login_files(self) -> BrowserAccountConfig:
        if self.login_mode == "email":
            if self.email_file is None or self.password_file is None:
                raise ValueError("email login requires email and password files")
        elif self.email_file is not None or self.password_file is not None:
            raise ValueError("manual login does not accept email or password files")
        return self


class KeeperSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    accounts: tuple[BrowserAccountConfig, ...] = Field(min_length=1)
    admin_token: SecretStr = Field(repr=False)
    state_path: str = "/data/keeper.db"
    profile_root: Path = Path("/profiles")
    probe_interval_seconds: float = Field(default=15 * 60, ge=10, le=24 * 60 * 60)
    scheduler_interval_seconds: float = Field(default=30, ge=1, le=60 * 60)
    action_timeout_seconds: float = Field(default=120, ge=5, le=15 * 60)
    retry_base_seconds: float = Field(default=60, ge=1, le=60 * 60)
    retry_max_seconds: float = Field(default=60 * 60, ge=1, le=24 * 60 * 60)

    @field_validator("admin_token")
    @classmethod
    def validate_admin_token(cls, value: SecretStr) -> SecretStr:
        token = value.get_secret_value().strip()
        if len(token) < 24:
            raise ValueError("admin token is too short")
        return SecretStr(token)

    @field_validator("profile_root")
    @classmethod
    def validate_profile_root(cls, value: Path) -> Path:
        return _absolute_path(value)

    @model_validator(mode="after")
    def validate_accounts(self) -> KeeperSettings:
        ids = [account.id for account in self.accounts]
        if len(ids) != len(set(ids)):
            raise ValueError("account ids must be unique")

        root = self.profile_root.resolve(strict=False)
        profile_paths = [
            account.profile_dir.resolve(strict=False) for account in self.accounts
        ]
        for profile_path in profile_paths:
            if not profile_path.is_relative_to(root):
                raise ValueError("profile directories must be below profile root")
        for index, left in enumerate(profile_paths):
            for right in profile_paths[index + 1 :]:
                if (
                    left == right
                    or left.is_relative_to(right)
                    or right.is_relative_to(left)
                ):
                    raise ValueError("profile directories must not overlap")
        if self.retry_base_seconds > self.retry_max_seconds:
            raise ValueError("retry base must not exceed retry maximum")
        return self


def read_secret(path: Path, label: str, *, min_length: int = 1) -> str:
    try:
        value = path.read_text(encoding="utf-8-sig").strip()
    except OSError:
        raise ConfigurationError(f"{label} file is unavailable") from None
    if len(value) < min_length:
        raise ConfigurationError(f"{label} file is invalid")
    return value


def read_proxy_options(config: ProxyFileConfig) -> dict[str, str]:
    server = read_secret(config.server_file, "proxy server", min_length=8)
    parsed = urlsplit(server)
    if parsed.scheme not in {"http", "https", "socks5"} or not parsed.hostname:
        raise ConfigurationError("proxy server file is invalid")
    if parsed.username is not None or parsed.password is not None:
        raise ConfigurationError("proxy authentication must use separate files")

    result = {"server": server}
    if config.username_file is not None and config.password_file is not None:
        result["username"] = read_secret(config.username_file, "proxy username")
        result["password"] = read_secret(config.password_file, "proxy password")
    return result


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = path.read_text(encoding="utf-8-sig")
    except OSError:
        raise ConfigurationError("keeper configuration file is unavailable") from None
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        raise ConfigurationError("keeper configuration is not valid JSON") from None
    if not isinstance(payload, dict):
        raise ConfigurationError("keeper configuration has an invalid structure")
    return payload


def _env_float(env: Mapping[str, str], name: str, default: object) -> float:
    try:
        return float(env.get(name, default))
    except (TypeError, ValueError):
        raise ConfigurationError(f"{name} must be a number") from None


def load_settings(environ: Mapping[str, str] | None = None) -> KeeperSettings:
    env = os.environ if environ is None else environ
    config_path = Path(env.get("QVK_CONFIG_FILE", "/run/config/keeper.json"))
    admin_token_path = Path(
        env.get("QVK_ADMIN_TOKEN_FILE", "/run/secrets/keeper_admin_token")
    )
    raw = _read_json(config_path)
    if "admin_token" in raw:
        raise ConfigurationError("admin token must use the external token file")
    raw.update(
        {
            "admin_token": read_secret(admin_token_path, "admin token", min_length=24),
            "state_path": env.get("QVK_STATE_PATH", "/data/keeper.db"),
            "profile_root": env.get("QVK_PROFILE_ROOT", "/profiles"),
            "probe_interval_seconds": _env_float(
                env,
                "QVK_PROBE_INTERVAL_SECONDS",
                raw.get("probe_interval_seconds", 15 * 60),
            ),
            "scheduler_interval_seconds": _env_float(
                env,
                "QVK_SCHEDULER_INTERVAL_SECONDS",
                raw.get("scheduler_interval_seconds", 30),
            ),
            "action_timeout_seconds": _env_float(
                env,
                "QVK_ACTION_TIMEOUT_SECONDS",
                raw.get("action_timeout_seconds", 120),
            ),
            "retry_base_seconds": _env_float(
                env,
                "QVK_RETRY_BASE_SECONDS",
                raw.get("retry_base_seconds", 60),
            ),
            "retry_max_seconds": _env_float(
                env,
                "QVK_RETRY_MAX_SECONDS",
                raw.get("retry_max_seconds", 60 * 60),
            ),
        }
    )
    try:
        return KeeperSettings.model_validate(raw)
    except (ValidationError, TypeError, ValueError):
        raise ConfigurationError("keeper configuration is invalid") from None
