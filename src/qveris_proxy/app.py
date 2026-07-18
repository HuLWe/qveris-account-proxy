from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from .admin import ADMIN_CONFIG_MAX_BYTES, AdminConfigError
from .admin_ui import router as admin_ui_router
from .bootstrap import (
    BOOTSTRAP_EXCHANGE_MAX_BYTES,
    BOOTSTRAP_TICKET_TTL_SECONDS,
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


app = create_app()
