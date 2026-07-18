from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from .routes import PUBLIC_OPERATIONS, QVERIS_API_VERSION

QVERIS_OPENAPI_URL = "https://qveris.ai/openapi/qveris-public-api.openapi.json"
_HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete"})


class OpenAPIContractError(RuntimeError):
    """The published API contract differs from the proxy allowlist."""


@dataclass(frozen=True, slots=True)
class ContractSummary:
    version: str
    operation_count: int


def fetch_public_openapi(*, timeout_seconds: float = 20.0) -> dict[str, Any]:
    request = urllib.request.Request(
        QVERIS_OPENAPI_URL,
        headers={"User-Agent": "qveris-account-proxy-contract-check/0.1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            payload = json.load(response)
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        raise OpenAPIContractError(
            "published OpenAPI document is unavailable"
        ) from None
    if not isinstance(payload, dict):
        raise OpenAPIContractError("published OpenAPI document is not an object")
    return payload


def validate_public_openapi(spec: dict[str, Any]) -> ContractSummary:
    info = spec.get("info")
    version = info.get("version") if isinstance(info, dict) else None
    if version != QVERIS_API_VERSION:
        raise OpenAPIContractError(
            f"OpenAPI version changed: expected {QVERIS_API_VERSION}, got {version!r}"
        )

    actual: set[tuple[str, str]] = set()
    auth_by_operation: dict[tuple[str, str], bool] = {}
    paths = spec.get("paths")
    if not isinstance(paths, dict):
        raise OpenAPIContractError("published OpenAPI paths are missing")
    inherited_security = spec.get("security")

    for raw_path, path_item in paths.items():
        if not isinstance(raw_path, str) or not isinstance(path_item, dict):
            continue
        path = raw_path.removeprefix("/")
        for method, operation in path_item.items():
            if method.lower() not in _HTTP_METHODS or not isinstance(operation, dict):
                continue
            key = (method.upper(), path)
            actual.add(key)
            security = operation.get("security", inherited_security)
            auth_by_operation[key] = bool(security)

    expected = set(PUBLIC_OPERATIONS)
    if actual != expected:
        missing = sorted(expected - actual)
        unexpected = sorted(actual - expected)
        raise OpenAPIContractError(
            f"OpenAPI operations changed: missing={missing!r}, unexpected={unexpected!r}"
        )

    expected_public = {("GET", "meta")}
    actual_public = {
        key for key, requires_auth in auth_by_operation.items() if not requires_auth
    }
    if actual_public != expected_public:
        raise OpenAPIContractError(
            f"OpenAPI authentication changed: public={sorted(actual_public)!r}"
        )

    return ContractSummary(version=version, operation_count=len(actual))
