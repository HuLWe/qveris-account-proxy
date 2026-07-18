from __future__ import annotations

from qveris_proxy.openapi_contract import (
    OpenAPIContractError,
    fetch_public_openapi,
    validate_public_openapi,
)


def main() -> None:
    summary = validate_public_openapi(fetch_public_openapi())
    print(
        f"QVeris OpenAPI {summary.version}: "
        f"{summary.operation_count} operations match the proxy allowlist"
    )


if __name__ == "__main__":
    try:
        main()
    except OpenAPIContractError as exc:
        raise SystemExit(str(exc)) from None
