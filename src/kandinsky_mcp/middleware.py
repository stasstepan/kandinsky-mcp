"""HTTP-поведение, которого требует профиль GigaCowork.

1. ``GET /mcp`` без сессии должен отвечать ``405 Method Not Allowed``
   (``SSE`` мы не поддерживаем: серверных уведомлений нет).
2. Если запрос пришёл с заголовком ``Origin``, он должен быть доверенным,
   иначе ``403 Forbidden`` — защита от DNS-rebinding и чужих браузерных
   страниц. Список доверенных Origin — ``MCP_ALLOWED_ORIGINS``.
"""

from __future__ import annotations

from fnmatch import fnmatch
from typing import Any

from starlette.responses import JSONResponse

Scope = dict[str, Any]
Receive = Any
Send = Any


class ProtocolCompatMiddleware:
    """ASGI-middleware: Origin allowlist и 405 на GET /mcp без сессии.

    Чистый ASGI (не BaseHTTPMiddleware): обёртка на BaseHTTPMiddleware
    буферизует поток и ломает SSE — та же причина, что в ``auth.py``.
    """

    def __init__(
        self,
        app: Any,
        *,
        mcp_path: str = "/mcp",
        allowed_origins: tuple[str, ...] = (),
    ) -> None:
        self.app = app
        self.mcp_path = "/" + mcp_path.strip("/")
        self.allowed_origins = tuple(item for item in allowed_origins if item)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http" or scope.get("path") != self.mcp_path:
            await self.app(scope, receive, send)
            return

        headers = {key.lower(): value.decode("latin-1") for key, value in scope.get("headers") or []}

        origin = headers.get(b"origin")
        if origin and not self._origin_allowed(origin):
            response = JSONResponse(
                {
                    "error": "forbidden_origin",
                    "hint": (
                        "Этот Origin не разрешён для MCP. Добавьте его в "
                        "MCP_ALLOWED_ORIGINS, если запрос легитимный."
                    ),
                },
                status_code=403,
            )
            await response(scope, receive, send)
            return

        if scope.get("method") == "GET" and not headers.get(b"mcp-session-id"):
            response = JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {
                        "code": -32600,
                        "message": "GET is not supported: use POST for JSON-RPC",
                    },
                },
                status_code=405,
                headers={"Allow": "POST, DELETE"},
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)

    def _origin_allowed(self, origin: str) -> bool:
        return any(pattern == "*" or fnmatch(origin, pattern) for pattern in self.allowed_origins)
