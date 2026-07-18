from __future__ import annotations

import hashlib
from functools import lru_cache
from importlib.resources import files

from fastapi import APIRouter, Request, Response
from fastapi.responses import RedirectResponse

router = APIRouter(include_in_schema=False)

_ASSETS = {
    "admin.css": "text/css; charset=utf-8",
    "admin.js": "text/javascript; charset=utf-8",
}
_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; "
        "connect-src 'self'; img-src 'self'; base-uri 'none'; "
        "frame-ancestors 'none'; form-action 'none'"
    ),
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


@lru_cache(maxsize=4)
def _resource(name: str) -> tuple[bytes, str]:
    content = files("qveris_proxy").joinpath("admin_assets", name).read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    return content, f'"{digest}"'


def _headers(*, cache_control: str) -> dict[str, str]:
    return {**_SECURITY_HEADERS, "Cache-Control": cache_control}


@router.get("/admin")
async def admin_redirect() -> RedirectResponse:
    return RedirectResponse(
        "/admin/",
        status_code=307,
        headers=_headers(cache_control="no-store"),
    )


@router.get("/admin/")
async def admin_shell() -> Response:
    content, _ = _resource("index.html")
    _, stylesheet_etag = _resource("admin.css")
    _, script_etag = _resource("admin.js")
    content = content.replace(
        b"__ADMIN_CSS_VERSION__", stylesheet_etag.strip('"').encode("ascii")
    ).replace(b"__ADMIN_JS_VERSION__", script_etag.strip('"').encode("ascii"))
    return Response(
        content,
        media_type="text/html",
        headers=_headers(cache_control="no-store"),
    )


@router.get("/admin/assets/{asset_name}")
async def admin_asset(asset_name: str, request: Request) -> Response:
    media_type = _ASSETS.get(asset_name)
    if media_type is None:
        return Response(status_code=404, headers=_headers(cache_control="no-store"))
    content, etag = _resource(asset_name)
    headers = {
        **_headers(cache_control="no-cache"),
        "ETag": etag,
    }
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return Response(content, media_type=media_type, headers=headers)
