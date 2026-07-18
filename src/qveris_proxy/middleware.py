from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any


class APIVersionHeaderMiddleware:
    def __init__(self, app: Callable[..., Awaitable[None]], version: str) -> None:
        self.app = app
        self._header = (b"x-qveris-api-version", version.encode("ascii"))

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        if scope.get("type") != "http" or not scope.get("path", "").startswith(
            "/api/v1/"
        ):
            await self.app(scope, receive, send)
            return

        async def send_with_version(message: dict[str, Any]) -> None:
            if message.get("type") == "http.response.start":
                headers = list(message.get("headers", []))
                if not any(name.lower() == self._header[0] for name, _ in headers):
                    headers.append(self._header)
                    message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_version)
