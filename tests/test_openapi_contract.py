from __future__ import annotations

import pytest

from qveris_proxy.openapi_contract import OpenAPIContractError, validate_public_openapi
from qveris_proxy.routes import PUBLIC_OPERATIONS, QVERIS_API_VERSION


def make_spec() -> dict[str, object]:
    paths: dict[str, object] = {}
    for method, path in PUBLIC_OPERATIONS:
        paths.setdefault(f"/{path}", {})[method.lower()] = {
            "security": [] if path == "meta" else [{"HTTPBearer": []}]
        }
    return {
        "info": {"version": QVERIS_API_VERSION},
        "paths": paths,
    }


def test_current_contract_is_accepted() -> None:
    summary = validate_public_openapi(make_spec())

    assert summary.version == QVERIS_API_VERSION
    assert summary.operation_count == 19


def test_version_drift_is_rejected() -> None:
    spec = make_spec()
    spec["info"]["version"] = "NEXT"

    with pytest.raises(OpenAPIContractError, match="version changed"):
        validate_public_openapi(spec)


def test_operation_drift_is_rejected() -> None:
    spec = make_spec()
    del spec["paths"]["/tools/execute"]

    with pytest.raises(OpenAPIContractError, match="operations changed"):
        validate_public_openapi(spec)


def test_authentication_drift_is_rejected() -> None:
    spec = make_spec()
    spec["paths"]["/search"]["post"]["security"] = []

    with pytest.raises(OpenAPIContractError, match="authentication changed"):
        validate_public_openapi(spec)
