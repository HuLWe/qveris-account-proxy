from __future__ import annotations

from collections.abc import Mapping

HOP_BY_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)

REQUEST_HEADER_ALLOWLIST = frozenset(
    {
        "accept",
        "content-type",
        "idempotency-key",
        "traceparent",
        "tracestate",
        "x-request-id",
    }
)

RESPONSE_HEADER_ALLOWLIST = frozenset(
    {
        "cache-control",
        "content-disposition",
        "content-encoding",
        "content-language",
        "content-length",
        "content-type",
        "etag",
        "last-modified",
        "retry-after",
        "traceparent",
        "x-ratelimit-limit",
        "x-ratelimit-remaining",
        "x-ratelimit-reset",
        "x-qveris-api-version",
        "x-request-id",
    }
)


def _connection_tokens(headers: Mapping[str, str]) -> set[str]:
    tokens: set[str] = set()
    for header_name in ("connection", "proxy-connection"):
        raw = headers.get(header_name, "")
        tokens.update(item.strip().lower() for item in raw.split(",") if item.strip())
    return tokens


def build_upstream_headers(
    headers: Mapping[str, str], api_key: str | None
) -> dict[str, str]:
    blocked = HOP_BY_HOP_HEADERS | _connection_tokens(headers)
    forwarded = {
        name.lower(): value
        for name, value in headers.items()
        if name.lower() in REQUEST_HEADER_ALLOWLIST and name.lower() not in blocked
    }
    if api_key is not None:
        forwarded["authorization"] = f"Bearer {api_key}"
    return forwarded


def build_downstream_headers(headers: Mapping[str, str]) -> dict[str, str]:
    blocked = HOP_BY_HOP_HEADERS | _connection_tokens(headers)
    return {
        name.lower(): value
        for name, value in headers.items()
        if name.lower() in RESPONSE_HEADER_ALLOWLIST and name.lower() not in blocked
    }
