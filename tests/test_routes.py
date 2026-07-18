from __future__ import annotations

import pytest

from qveris_proxy.routes import PUBLIC_OPERATIONS, resolve_operation


EXPECTED_OPERATIONS = {
    ("GET", "auth/account/settlement-history"),
    ("GET", "auth/credits"),
    ("GET", "auth/credits/ledger"),
    ("GET", "auth/credits/ledger-readiness"),
    ("GET", "auth/credits/ledger/export"),
    ("GET", "auth/credits/ledger/{entry_id}"),
    ("GET", "auth/usage/credits-spent"),
    ("GET", "auth/usage/history/v2"),
    ("GET", "auth/usage/history/v2/export"),
    ("GET", "auth/usage/history/v2/summary"),
    ("GET", "auth/usage/history/v2/{event_id}"),
    ("POST", "auth/verify-token"),
    ("GET", "credits"),
    ("GET", "meta"),
    ("GET", "providers"),
    ("GET", "providers/categories"),
    ("POST", "search"),
    ("POST", "tools/by-ids"),
    ("POST", "tools/execute"),
}


def materialize_path(template: str) -> str:
    return template.replace("{entry_id}", "led_01ABCDEF").replace(
        "{event_id}", "123e4567-e89b-42d3-a456-426614174000"
    )


def test_operation_table_matches_full_public_openapi_contract() -> None:
    assert set(PUBLIC_OPERATIONS) == EXPECTED_OPERATIONS
    assert len(PUBLIC_OPERATIONS) == 19


@pytest.mark.parametrize("method,path_template", sorted(EXPECTED_OPERATIONS))
def test_every_public_operation_resolves(method: str, path_template: str) -> None:
    path = materialize_path(path_template)
    operation = resolve_operation(method, path)

    assert operation is not None
    assert operation.upstream_path == path
    if path == "meta":
        assert operation.provider_auth is False
        assert operation.proxy_auth is False
    else:
        assert operation.provider_auth is True
        assert operation.proxy_auth is True


@pytest.mark.parametrize(
    "path",
    [
        "auth/credits",
        "providers",
        "providers/categories",
        "search",
        "tools/by-ids",
        "tools/execute",
    ],
)
def test_public_api_routes_require_api_keys(path: str) -> None:
    method = "POST" if path in {"search", "tools/by-ids", "tools/execute"} else "GET"
    operation = resolve_operation(method, path)
    assert operation is not None
    assert operation.credential_kind == "api_key"


@pytest.mark.parametrize(
    "path",
    [
        "auth/account/settlement-history",
        "auth/credits/ledger",
        "auth/usage/history/v2",
        "auth/verify-token",
        "credits",
    ],
)
def test_account_routes_require_oauth_tokens(path: str) -> None:
    method = "POST" if path == "auth/verify-token" else "GET"
    operation = resolve_operation(method, path)
    assert operation is not None
    assert operation.credential_kind == "oauth"


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "http://169.254.169.254/latest/meta-data"),
        ("GET", "auth/credits/ledger/../../meta"),
        ("GET", "auth/usage/history/v2/not-a-uuid"),
        ("POST", "meta"),
    ],
)
def test_unknown_or_malformed_operations_do_not_resolve(method: str, path: str) -> None:
    assert resolve_operation(method, path) is None
