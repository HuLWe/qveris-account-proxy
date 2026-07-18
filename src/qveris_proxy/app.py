from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import TypeVar

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .access_keys import (
    PrimaryProxyAccessKeyRequired,
    PROXY_ACCESS_KEY_CONCURRENCY_MAX,
    PROXY_ACCESS_KEY_EXPIRES_AT_MAX,
    PROXY_ACCESS_KEY_NAME_MAX_LENGTH,
    PROXY_ACCESS_KEY_REQUEST_LIMIT_MAX,
    PROXY_ACCESS_KEY_RPM_MAX,
    ProxyAccessKeyNotFound,
)
from .admin import ADMIN_CONFIG_MAX_BYTES, AdminConfigError
from .admin_ui import router as admin_ui_router
from .bootstrap import (
    ADMIN_BROWSER_SESSION_COOKIE,
    ADMIN_BROWSER_SESSION_HEADER,
    BOOTSTRAP_EXCHANGE_MAX_BYTES,
    BOOTSTRAP_TICKET_TTL_SECONDS,
    AdminBrowserSessions,
    AdminBootstrapTickets,
    BootstrapExchangeInput,
    BootstrapTicketCapacityError,
)
from .config import ProxySettings, load_settings
from .middleware import APIVersionHeaderMiddleware
from .routes import (
    QVERIS_API_VERSION,
    allowed_methods,
    public_operation_catalog,
    resolve_operation,
)
from .service import ProxyService

logger = logging.getLogger(__name__)
_BOOTSTRAP_NO_STORE_HEADERS = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
}
_PROXY_KEY_PAYLOAD_MAX_BYTES = 16 * 1024
_ModelT = TypeVar("_ModelT", bound=BaseModel)


class _ProxyKeyCreateInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: str = Field(min_length=1, max_length=PROXY_ACCESS_KEY_NAME_MAX_LENGTH)
    enabled: bool = True
    request_limit: int | None = Field(
        default=None, ge=1, le=PROXY_ACCESS_KEY_REQUEST_LIMIT_MAX
    )
    requests_per_minute: int | None = Field(
        default=None, ge=1, le=PROXY_ACCESS_KEY_RPM_MAX
    )
    max_concurrency: int = Field(default=8, ge=1, le=PROXY_ACCESS_KEY_CONCURRENCY_MAX)
    expires_at: int | float | None = Field(
        default=None,
        gt=0,
        le=PROXY_ACCESS_KEY_EXPIRES_AT_MAX,
    )

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("name must not be blank")
        return normalized


class _ProxyKeyUpdateInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: str = Field(
        default="", min_length=1, max_length=PROXY_ACCESS_KEY_NAME_MAX_LENGTH
    )
    enabled: bool = False
    request_limit: int | None = Field(
        default=None, ge=1, le=PROXY_ACCESS_KEY_REQUEST_LIMIT_MAX
    )
    requests_per_minute: int | None = Field(
        default=None, ge=1, le=PROXY_ACCESS_KEY_RPM_MAX
    )
    max_concurrency: int = Field(default=8, ge=1, le=PROXY_ACCESS_KEY_CONCURRENCY_MAX)
    expires_at: int | float | None = Field(
        default=None,
        gt=0,
        le=PROXY_ACCESS_KEY_EXPIRES_AT_MAX,
    )

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("name must not be blank")
        return normalized


def create_app(
    settings: ProxySettings | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        resolved_settings = settings if settings is not None else load_settings()
        service = ProxyService(resolved_settings, transport=transport)
        application.state.admin_bootstrap_tickets.clear()
        application.state.admin_browser_sessions = AdminBrowserSessions(
            resolved_settings.proxy_access_token.get_secret_value()
        )
        await service.start()
        application.state.proxy_service = service
        background_tasks: list[asyncio.Task[None]] = []
        credential_reload_stop: asyncio.Event | None = None
        if resolved_settings.quota_refresh_interval_seconds > 0:

            async def quota_loop() -> None:
                while True:
                    try:
                        await service.refresh_quotas()
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        logger.warning(
                            "background quota refresh failed: %s",
                            type(exc).__name__,
                        )
                    await asyncio.sleep(
                        resolved_settings.quota_refresh_interval_seconds
                    )

            background_tasks.append(asyncio.create_task(quota_loop()))
        if service.credential_reload_background_enabled:
            credential_reload_stop = asyncio.Event()
            background_tasks.append(
                asyncio.create_task(
                    service.run_credential_reloader(credential_reload_stop)
                )
            )
        try:
            yield
        finally:
            application.state.admin_bootstrap_tickets.clear()
            if credential_reload_stop is not None:
                credential_reload_stop.set()
            for task in background_tasks:
                task.cancel()
            if background_tasks:
                await asyncio.gather(*background_tasks, return_exceptions=True)
            await service.close()

    application = FastAPI(
        title="QVeris Account Proxy",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    application.add_middleware(APIVersionHeaderMiddleware, version=QVERIS_API_VERSION)
    application.include_router(admin_ui_router)
    application.state.admin_bootstrap_tickets = AdminBootstrapTickets()

    @application.get("/health/live", include_in_schema=False)
    async def health_live() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/health/ready", include_in_schema=False)
    async def health_ready(request: Request) -> JSONResponse:
        ready = await request.app.state.proxy_service.pool.is_ready()
        status_code = 200 if ready else 503
        status = "ready" if ready else "degraded"
        return JSONResponse({"status": status}, status_code=status_code)

    @application.get("/admin/v1/accounts", include_in_schema=False)
    async def account_status(request: Request, response: Response) -> dict[str, object]:
        service = request.app.state.proxy_service
        try:
            service.authenticate(request)
        except HTTPException as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail=exc.detail,
                headers={**(exc.headers or {}), **_BOOTSTRAP_NO_STORE_HEADERS},
            ) from None
        response.headers["Cache-Control"] = "no-store"
        return {
            "accounts": await service.account_status(),
            "credential_reload": asdict(await service.credential_reload_status()),
        }

    @application.get("/admin/v1/proxy-keys", include_in_schema=False)
    async def list_proxy_keys(
        request: Request, response: Response
    ) -> dict[str, object]:
        service = request.app.state.proxy_service
        service.authenticate(request)
        response.headers["Cache-Control"] = "no-store"
        return {"keys": [asdict(key) for key in await service.proxy_access_keys.list()]}

    @application.post("/admin/v1/proxy-keys", include_in_schema=False)
    async def create_proxy_key(
        request: Request, response: Response
    ) -> dict[str, object]:
        service = request.app.state.proxy_service
        service.authenticate(request)
        response.headers.update(_BOOTSTRAP_NO_STORE_HEADERS)
        payload = await _read_proxy_key_payload(request, _ProxyKeyCreateInput)
        try:
            created = await service.proxy_access_keys.create(
                payload.name,
                enabled=payload.enabled,
                request_limit=payload.request_limit,
                requests_per_minute=payload.requests_per_minute,
                max_concurrency=payload.max_concurrency,
                expires_at=payload.expires_at,
            )
        except ValueError:
            _raise_proxy_key_error("invalid_proxy_access_key")
        return {"key": asdict(created.key), "secret": created.secret}

    @application.patch("/admin/v1/proxy-keys/{key_id}", include_in_schema=False)
    async def update_proxy_key(
        key_id: str, request: Request, response: Response
    ) -> dict[str, object]:
        service = request.app.state.proxy_service
        service.authenticate(request)
        response.headers["Cache-Control"] = "no-store"
        payload = await _read_proxy_key_payload(request, _ProxyKeyUpdateInput)
        if not payload.model_fields_set:
            _raise_proxy_key_error("empty_proxy_access_key_update")
        changes = {field: getattr(payload, field) for field in payload.model_fields_set}
        try:
            key = await service.proxy_access_keys.update(key_id, **changes)
        except (ProxyAccessKeyNotFound, ValueError) as exc:
            _raise_proxy_key_exception(exc)
        return {"key": asdict(key)}

    @application.delete("/admin/v1/proxy-keys/{key_id}", include_in_schema=False)
    async def delete_proxy_key(
        key_id: str, request: Request, response: Response
    ) -> dict[str, str]:
        service = request.app.state.proxy_service
        service.authenticate(request)
        response.headers["Cache-Control"] = "no-store"
        try:
            await service.proxy_access_keys.delete(key_id)
        except (ProxyAccessKeyNotFound, PrimaryProxyAccessKeyRequired) as exc:
            _raise_proxy_key_exception(exc)
        return {"deleted": key_id}

    @application.post(
        "/admin/v1/proxy-keys/{key_id}/reset-usage",
        include_in_schema=False,
    )
    async def reset_proxy_key_usage(
        key_id: str, request: Request, response: Response
    ) -> dict[str, object]:
        service = request.app.state.proxy_service
        service.authenticate(request)
        response.headers["Cache-Control"] = "no-store"
        try:
            key = await service.proxy_access_keys.reset_usage(key_id)
        except ProxyAccessKeyNotFound as exc:
            _raise_proxy_key_exception(exc)
        return {"key": asdict(key)}

    @application.post("/admin/v1/bootstrap-ticket", include_in_schema=False)
    async def create_admin_bootstrap_ticket(
        request: Request, response: Response
    ) -> dict[str, object]:
        service = request.app.state.proxy_service
        service.authenticate(request)
        response.headers["Cache-Control"] = "no-store"
        try:
            ticket = request.app.state.admin_bootstrap_tickets.issue()
        except BootstrapTicketCapacityError:
            raise HTTPException(
                status_code=503,
                detail="bootstrap ticket capacity reached",
                headers=_BOOTSTRAP_NO_STORE_HEADERS,
            ) from None
        return {"ticket": ticket, "expires_in": BOOTSTRAP_TICKET_TTL_SECONDS}

    @application.post("/admin/v1/bootstrap/exchange", include_in_schema=False)
    async def exchange_admin_bootstrap_ticket(
        request: Request, response: Response
    ) -> dict[str, str]:
        payload = await _read_bootstrap_exchange(request)
        if not request.app.state.admin_bootstrap_tickets.consume(payload.ticket):
            raise HTTPException(
                status_code=401,
                detail="bootstrap ticket invalid or expired",
                headers=_BOOTSTRAP_NO_STORE_HEADERS,
            )
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        return {
            "access_token": request.app.state.proxy_service.settings.proxy_access_token.get_secret_value()
        }

    @application.post("/admin/v1/browser-session", include_in_schema=False)
    async def create_admin_browser_session(
        request: Request, response: Response
    ) -> dict[str, str]:
        service = request.app.state.proxy_service
        service.authenticate(request)
        return _issue_admin_browser_session(request, response)

    @application.get("/admin/v1/browser-session", include_in_schema=False)
    async def resume_admin_browser_session(
        request: Request, response: Response
    ) -> dict[str, str]:
        _require_admin_browser_request(request)
        sessions = request.app.state.admin_browser_sessions
        candidate = request.cookies.get(ADMIN_BROWSER_SESSION_COOKIE, "")
        if not sessions.validate(candidate):
            raise HTTPException(
                status_code=401,
                detail="admin browser session invalid or expired",
                headers=_BOOTSTRAP_NO_STORE_HEADERS,
            )
        return _issue_admin_browser_session(request, response)

    @application.post("/admin/v1/browser-session/claim", include_in_schema=False)
    async def claim_admin_browser_session(
        request: Request, response: Response
    ) -> dict[str, str]:
        _require_admin_browser_request(request)
        service = request.app.state.proxy_service
        if not service.settings.admin_first_open_claim_enabled:
            raise HTTPException(
                status_code=403,
                detail="first-open browser claim disabled",
                headers=_BOOTSTRAP_NO_STORE_HEADERS,
            )
        sessions = request.app.state.admin_browser_sessions
        if not await service.state.claim_admin_browser(sessions.claim_key):
            raise HTTPException(
                status_code=409,
                detail="first-open browser claim already used",
                headers=_BOOTSTRAP_NO_STORE_HEADERS,
            )
        return _issue_admin_browser_session(request, response)

    @application.delete("/admin/v1/browser-session", include_in_schema=False)
    async def clear_admin_browser_session(
        request: Request, response: Response
    ) -> dict[str, bool]:
        response.headers.update(_BOOTSTRAP_NO_STORE_HEADERS)
        response.delete_cookie(
            ADMIN_BROWSER_SESSION_COOKIE,
            path="/admin",
            secure=request.url.scheme == "https",
            httponly=True,
            samesite="strict",
        )
        return {"disconnected": True}

    @application.get("/admin/v1/config", include_in_schema=False)
    async def admin_config(request: Request, response: Response) -> dict[str, object]:
        service = request.app.state.proxy_service
        service.authenticate(request)
        response.headers["Cache-Control"] = "no-store"
        config = service.admin_config()
        config["revision"] = (await service.credential_reload_status()).generation
        return config

    @application.post("/admin/v1/config/validate", include_in_schema=False)
    async def validate_admin_config(
        request: Request, response: Response
    ) -> dict[str, object]:
        service = request.app.state.proxy_service
        service.authenticate(request)
        response.headers["Cache-Control"] = "no-store"
        payload = await _read_admin_payload(request)
        try:
            return service.validate_admin_config(payload)
        except AdminConfigError as exc:
            _raise_admin_config_error(str(exc))

    @application.put("/admin/v1/config", include_in_schema=False)
    async def save_admin_config(
        request: Request, response: Response
    ) -> dict[str, object]:
        service = request.app.state.proxy_service
        service.authenticate(request)
        response.headers["Cache-Control"] = "no-store"
        payload = await _read_admin_payload(request)
        try:
            result = await service.save_admin_config(payload)
        except AdminConfigError as exc:
            _raise_admin_config_error(str(exc))
        return {
            "config": service.admin_config(),
            "reload": asdict(result),
        }

    @application.get("/admin/v1/operations", include_in_schema=False)
    async def admin_operations(
        request: Request, response: Response
    ) -> dict[str, object]:
        service = request.app.state.proxy_service
        service.authenticate(request)
        response.headers["Cache-Control"] = "no-store"
        return {
            "api_version": QVERIS_API_VERSION,
            "operations": public_operation_catalog(),
        }

    @application.post("/admin/v1/accounts/{account_id}/test", include_in_schema=False)
    async def test_account(
        account_id: str, request: Request, response: Response
    ) -> dict[str, object]:
        service = request.app.state.proxy_service
        service.authenticate(request)
        response.headers["Cache-Control"] = "no-store"
        try:
            return await service.test_account(account_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="account not found") from None

    @application.delete("/admin/v1/accounts/{account_id}", include_in_schema=False)
    async def delete_account(
        account_id: str, request: Request, response: Response
    ) -> dict[str, object]:
        service = request.app.state.proxy_service
        service.authenticate(request)
        response.headers["Cache-Control"] = "no-store"
        try:
            result = await service.delete_admin_account(account_id)
        except AdminConfigError as exc:
            _raise_admin_config_error(str(exc))
        return {"deleted": account_id, "reload": asdict(result)}

    @application.post("/admin/v1/refresh-credits", include_in_schema=False)
    async def refresh_credits(
        request: Request, response: Response
    ) -> dict[str, object]:
        service = request.app.state.proxy_service
        service.authenticate(request)
        response.headers["Cache-Control"] = "no-store"
        return {"accounts": await service.refresh_quotas()}

    @application.post("/admin/v1/reload-accounts", include_in_schema=False)
    async def reload_accounts(
        request: Request, response: Response
    ) -> dict[str, object]:
        service = request.app.state.proxy_service
        service.authenticate(request)
        response.headers["Cache-Control"] = "no-store"
        result = await service.reload_accounts(force=True)
        if result.error is not None:
            raise HTTPException(
                status_code=409,
                detail=f"credential reload failed: {result.error}",
            )
        return {"reload": asdict(result)}

    @application.api_route("/api/v1/{api_path:path}", methods=["GET", "POST"])
    async def qveris_api(api_path: str, request: Request):
        operation = resolve_operation(request.method, api_path)
        if operation is None:
            methods = allowed_methods(api_path)
            if methods:
                raise HTTPException(
                    status_code=405,
                    detail="method not allowed",
                    headers={"Allow": ", ".join(methods)},
                )
            raise HTTPException(status_code=404, detail="unknown QVeris API route")
        return await request.app.state.proxy_service.forward(request, operation)

    return application


async def _read_admin_payload(request: Request) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared = int(content_length)
        except ValueError:
            raise HTTPException(
                status_code=400, detail="invalid Content-Length"
            ) from None
        if declared < 0:
            raise HTTPException(status_code=400, detail="invalid Content-Length")
        if declared > ADMIN_CONFIG_MAX_BYTES:
            raise HTTPException(status_code=413, detail="configuration is too large")

    payload = bytearray()
    async for chunk in request.stream():
        payload.extend(chunk)
        if len(payload) > ADMIN_CONFIG_MAX_BYTES:
            raise HTTPException(status_code=413, detail="configuration is too large")
    return bytes(payload)


async def _read_proxy_key_payload(request: Request, model: type[_ModelT]) -> _ModelT:
    media_type = (
        request.headers.get("content-type", "").partition(";")[0].strip().lower()
    )
    if media_type != "application/json":
        raise HTTPException(
            status_code=415,
            detail="proxy API key settings must use application/json",
        )
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared = int(content_length)
        except ValueError:
            raise HTTPException(
                status_code=400, detail="invalid Content-Length"
            ) from None
        if declared < 0:
            raise HTTPException(status_code=400, detail="invalid Content-Length")
        if declared > _PROXY_KEY_PAYLOAD_MAX_BYTES:
            raise HTTPException(
                status_code=413, detail="proxy API key settings are too large"
            )

    payload = bytearray()
    async for chunk in request.stream():
        payload.extend(chunk)
        if len(payload) > _PROXY_KEY_PAYLOAD_MAX_BYTES:
            raise HTTPException(
                status_code=413, detail="proxy API key settings are too large"
            )
    try:
        return model.model_validate_json(bytes(payload))
    except ValidationError:
        _raise_proxy_key_error("invalid_proxy_access_key")
    raise AssertionError("unreachable")


def _raise_proxy_key_exception(exc: Exception) -> None:
    if isinstance(exc, ProxyAccessKeyNotFound):
        _raise_proxy_key_error(exc.code)
    if isinstance(exc, PrimaryProxyAccessKeyRequired):
        _raise_proxy_key_error(exc.code)
    _raise_proxy_key_error("invalid_proxy_access_key")


def _raise_proxy_key_error(code: str) -> None:
    status_code = {
        ProxyAccessKeyNotFound.code: 404,
        PrimaryProxyAccessKeyRequired.code: 409,
        "empty_proxy_access_key_update": 400,
        "invalid_proxy_access_key": 400,
    }.get(code, 400)
    raise HTTPException(status_code=status_code, detail=code)


async def _read_bootstrap_exchange(request: Request) -> BootstrapExchangeInput:
    if request.headers.get("x-qveris-bootstrap") != "1":
        raise HTTPException(
            status_code=400,
            detail="invalid bootstrap request",
            headers=_BOOTSTRAP_NO_STORE_HEADERS,
        )
    media_type = (
        request.headers.get("content-type", "").partition(";")[0].strip().lower()
    )
    if media_type != "application/json":
        raise HTTPException(
            status_code=415,
            detail="bootstrap request must use application/json",
            headers=_BOOTSTRAP_NO_STORE_HEADERS,
        )
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared = int(content_length)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail="invalid Content-Length",
                headers=_BOOTSTRAP_NO_STORE_HEADERS,
            ) from None
        if declared < 0:
            raise HTTPException(
                status_code=400,
                detail="invalid Content-Length",
                headers=_BOOTSTRAP_NO_STORE_HEADERS,
            )
        if declared > BOOTSTRAP_EXCHANGE_MAX_BYTES:
            raise HTTPException(
                status_code=413,
                detail="bootstrap request is too large",
                headers=_BOOTSTRAP_NO_STORE_HEADERS,
            )

    payload = bytearray()
    async for chunk in request.stream():
        payload.extend(chunk)
        if len(payload) > BOOTSTRAP_EXCHANGE_MAX_BYTES:
            raise HTTPException(
                status_code=413,
                detail="bootstrap request is too large",
                headers=_BOOTSTRAP_NO_STORE_HEADERS,
            )
    try:
        document = json.loads(payload.decode("utf-8"))
        return BootstrapExchangeInput.model_validate(document)
    except (UnicodeDecodeError, json.JSONDecodeError, ValidationError):
        raise HTTPException(
            status_code=400,
            detail="invalid bootstrap request",
            headers=_BOOTSTRAP_NO_STORE_HEADERS,
        ) from None


def _raise_admin_config_error(code: str) -> None:
    status_code = {
        "account_not_found": 404,
        "persistent_editing_disabled": 403,
        "accounts_file_unavailable": 409,
        "last_account_required": 409,
        "default_account_locked": 409,
        "config_too_large": 413,
        "invalid_config": 400,
        "missing_api_key_value": 400,
        "missing_oauth_value": 400,
        "write_failed": 409,
        "apply_failed": 409,
        "apply_and_rollback_failed": 500,
    }.get(code, 400)
    raise HTTPException(status_code=status_code, detail=code)


def _require_admin_browser_request(request: Request) -> None:
    if request.headers.get(ADMIN_BROWSER_SESSION_HEADER) != "1":
        raise HTTPException(
            status_code=400,
            detail="invalid admin browser session request",
            headers=_BOOTSTRAP_NO_STORE_HEADERS,
        )


def _issue_admin_browser_session(
    request: Request, response: Response
) -> dict[str, str]:
    sessions = request.app.state.admin_browser_sessions
    response.headers.update(_BOOTSTRAP_NO_STORE_HEADERS)
    response.set_cookie(
        ADMIN_BROWSER_SESSION_COOKIE,
        sessions.issue(),
        max_age=sessions.max_age_seconds,
        path="/admin",
        secure=request.url.scheme == "https",
        httponly=True,
        samesite="strict",
    )
    return {
        "access_token": request.app.state.proxy_service.settings.proxy_access_token.get_secret_value()
    }


app = create_app()
