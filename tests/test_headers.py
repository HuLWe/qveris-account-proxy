from __future__ import annotations

from qveris_proxy.headers import build_downstream_headers, build_upstream_headers
from conftest import KEY_A1


def test_upstream_headers_replace_credentials_and_strip_forwarding_headers() -> None:
    incoming = {
        "authorization": "Bearer client-secret",
        "cookie": "session=sensitive",
        "host": "attacker.invalid",
        "x-forwarded-host": "attacker.invalid",
        "connection": "X-Internal, keep-alive",
        "x-internal": "drop-me",
        "content-type": "application/json",
        "accept": "application/json",
        "x-request-id": "request-1",
    }

    result = build_upstream_headers(incoming, KEY_A1)

    assert result["authorization"] == f"Bearer {KEY_A1}"
    assert result["content-type"] == "application/json"
    assert result["x-request-id"] == "request-1"
    assert "cookie" not in result
    assert "host" not in result
    assert "x-forwarded-host" not in result
    assert "connection" not in result
    assert "x-internal" not in result


def test_downstream_headers_use_allowlist_and_dynamic_connection_filter() -> None:
    upstream = {
        "content-type": "application/json",
        "content-length": "12",
        "connection": "X-Internal",
        "x-internal": "secret-debug-value",
        "set-cookie": "session=sensitive",
        "www-authenticate": "Bearer upstream",
        "x-ratelimit-remaining": "7",
    }

    result = build_downstream_headers(upstream)

    assert result == {
        "content-type": "application/json",
        "content-length": "12",
        "x-ratelimit-remaining": "7",
    }


def test_public_upstream_request_does_not_forward_client_authorization() -> None:
    result = build_upstream_headers(
        {
            "authorization": "Bearer client-proxy-token",
            "accept": "application/json",
        },
        None,
    )

    assert result["accept"] == "application/json"
    assert "authorization" not in result
