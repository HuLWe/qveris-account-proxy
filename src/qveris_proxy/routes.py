from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

QVERIS_API_VERSION = "2026-07-17.2"


@dataclass(frozen=True, slots=True)
class Operation:
    method: str
    upstream_path: str
    route_id: str
    credential_kind: Literal["api_key", "oauth"] | None = "api_key"
    proxy_auth: bool = True
    auto_route: bool = False
    credit_sensitive: bool = False

    @property
    def provider_auth(self) -> bool:
        return self.credential_kind is not None


_STATIC_OPERATIONS = frozenset(
    {
        ("GET", "auth/account/settlement-history"),
        ("GET", "auth/credits"),
        ("GET", "auth/credits/ledger"),
        ("GET", "auth/credits/ledger-readiness"),
        ("GET", "auth/credits/ledger/export"),
        ("GET", "auth/usage/credits-spent"),
        ("GET", "auth/usage/history/v2"),
        ("GET", "auth/usage/history/v2/export"),
        ("GET", "auth/usage/history/v2/summary"),
        ("POST", "auth/verify-token"),
        ("GET", "credits"),
        ("GET", "meta"),
        ("GET", "providers"),
        ("GET", "providers/categories"),
        ("POST", "search"),
        ("POST", "tools/by-ids"),
        ("POST", "tools/execute"),
    }
)

_API_KEY_PATHS = frozenset(
    {
        "auth/credits",
        "providers",
        "providers/categories",
        "search",
        "tools/by-ids",
        "tools/execute",
    }
)

_LEDGER_ENTRY = re.compile(r"^auth/credits/ledger/[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$")
_USAGE_EVENT = re.compile(
    r"^auth/usage/history/v2/"
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
    r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
)

PUBLIC_OPERATIONS = tuple(
    sorted(
        _STATIC_OPERATIONS
        | {
            ("GET", "auth/credits/ledger/{entry_id}"),
            ("GET", "auth/usage/history/v2/{event_id}"),
        }
    )
)


def resolve_operation(method: str, path: str) -> Operation | None:
    normalized_method = method.upper()
    if (normalized_method, path) in _STATIC_OPERATIONS:
        is_public_meta = normalized_method == "GET" and path == "meta"
        return Operation(
            method=normalized_method,
            upstream_path=path,
            route_id=path,
            credential_kind=(
                None
                if is_public_meta
                else "api_key"
                if path in _API_KEY_PATHS
                else "oauth"
            ),
            proxy_auth=not is_public_meta,
            auto_route=path
            in {
                "providers",
                "providers/categories",
                "search",
                "tools/by-ids",
                "tools/execute",
            },
            credit_sensitive=path in {"search", "tools/execute"},
        )

    if normalized_method == "GET" and _LEDGER_ENTRY.fullmatch(path):
        return Operation(
            method="GET",
            upstream_path=path,
            route_id="auth/credits/ledger/{entry_id}",
            credential_kind="oauth",
        )
    if normalized_method == "GET" and _USAGE_EVENT.fullmatch(path):
        return Operation(
            method="GET",
            upstream_path=path,
            route_id="auth/usage/history/v2/{event_id}",
            credential_kind="oauth",
        )
    return None


def allowed_methods(path: str) -> tuple[str, ...]:
    methods = tuple(
        method
        for method in ("GET", "POST")
        if resolve_operation(method, path) is not None
    )
    return methods


def public_operation_catalog() -> list[dict[str, object]]:
    materialized_parameters = {
        "{entry_id}": "ENTRY_ID",
        "{event_id}": "00000000-0000-4000-8000-000000000000",
    }
    catalog: list[dict[str, object]] = []
    for method, path in PUBLIC_OPERATIONS:
        materialized_path = path
        for placeholder, value in materialized_parameters.items():
            materialized_path = materialized_path.replace(placeholder, value)
        operation = resolve_operation(method, materialized_path)
        assert operation is not None
        catalog.append(
            {
                "method": method,
                "path": path,
                "credential_kind": operation.credential_kind,
                "auto_route": operation.auto_route,
                "credit_sensitive": operation.credit_sensitive,
            }
        )
    return catalog
