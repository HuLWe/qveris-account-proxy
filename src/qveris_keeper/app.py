from __future__ import annotations

import hmac
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from .browser import BrowserRuntime
from .config import KeeperSettings, load_settings
from .service import KeeperService


def create_app(
    settings: KeeperSettings | None = None,
    *,
    runtime: BrowserRuntime | None = None,
    wall_time: Callable[[], float] | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        resolved_settings = settings if settings is not None else load_settings()
        kwargs = {"runtime": runtime}
        if wall_time is not None:
            kwargs["wall_time"] = wall_time
        service = KeeperService(resolved_settings, **kwargs)
        application.state.keeper_service = service
        try:
            await service.start()
            yield
        finally:
            await service.close()

    application = FastAPI(
        title="QVeris Session Keeper",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    def authenticate(request: Request) -> None:
        expected = (
            request.app.state.keeper_service.settings.admin_token.get_secret_value()
        )
        scheme, separator, token = request.headers.get("authorization", "").partition(
            " "
        )
        valid = (
            bool(separator)
            and scheme.lower() == "bearer"
            and hmac.compare_digest(token.strip(), expected)
        )
        if not valid:
            raise HTTPException(
                status_code=401,
                detail="authentication required",
                headers={"WWW-Authenticate": "Bearer"},
            )

    @application.get("/health/live", include_in_schema=False)
    async def health_live() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/health/ready", include_in_schema=False)
    async def health_ready(request: Request) -> JSONResponse:
        ready = request.app.state.keeper_service.is_ready()
        return JSONResponse(
            {"status": "ready" if ready else "degraded"},
            status_code=200 if ready else 503,
        )

    @application.get("/admin/v1/accounts", include_in_schema=False)
    async def account_status(request: Request, response: Response) -> dict[str, object]:
        authenticate(request)
        response.headers["Cache-Control"] = "no-store"
        service = request.app.state.keeper_service
        return {"accounts": service.account_status()}

    @application.post("/admin/v1/refresh", include_in_schema=False)
    async def refresh_all(request: Request, response: Response) -> dict[str, object]:
        authenticate(request)
        response.headers["Cache-Control"] = "no-store"
        service = request.app.state.keeper_service
        return {"accounts": await service.refresh_all()}

    @application.post(
        "/admin/v1/accounts/{account_id}/refresh", include_in_schema=False
    )
    async def refresh_account(
        account_id: str, request: Request, response: Response
    ) -> dict[str, object]:
        authenticate(request)
        response.headers["Cache-Control"] = "no-store"
        service = request.app.state.keeper_service
        try:
            account = await service.refresh_account(account_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="unknown account") from None
        return {"account": account}

    @application.post("/admin/v1/accounts/{account_id}/touch", include_in_schema=False)
    async def touch_account(
        account_id: str, request: Request, response: Response
    ) -> dict[str, object]:
        authenticate(request)
        response.headers["Cache-Control"] = "no-store"
        service = request.app.state.keeper_service
        try:
            account = await service.touch_account(account_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="unknown account") from None
        return {"account": account}

    return application


app = create_app()
