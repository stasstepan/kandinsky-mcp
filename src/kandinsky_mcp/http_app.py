"""HTTP-приложение: MCP по /mcp, файлы по /files, health-проверки.

Собирается из приложения, которое отдаёт сам MCP SDK, плюс собственные роуты
и два middleware: ключ доступа и ограничение частоты.
"""

from __future__ import annotations

import calendar
import time
from pathlib import Path
from typing import Any

from mcp.server import MCPServer
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response

from . import __version__
from .api.errors import KandinskyError
from .auth import ApiKeyMiddleware, RateLimiter, RateLimitMiddleware, verify_file
from .tools.context import AppContext

# Кеш проверки готовности: не дёргаем внешний API на каждый запрос.
_READY_TTL_SECONDS = 30.0


class ReadinessProbe:
    def __init__(self, ctx: AppContext) -> None:
        self._ctx = ctx
        self._checked_at = 0.0
        self._healthy = False

    async def check(self) -> tuple[bool, bool]:
        """Возвращает (healthy, from_cache)."""
        now = time.monotonic()
        if now - self._checked_at < _READY_TTL_SECONDS:
            return self._healthy, True
        self._healthy = await self._ctx.client.health()
        self._checked_at = now
        return self._healthy, False


def build_app(mcp: MCPServer, ctx: AppContext) -> Starlette:
    settings = ctx.settings
    probe = ReadinessProbe(ctx)

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(request: Request) -> Response:
        return JSONResponse({"status": "ok", "version": __version__, "transport": "http"})

    @mcp.custom_route("/readyz", methods=["GET"])
    async def readyz(request: Request) -> Response:
        healthy, cached = await probe.check()
        body = {
            "status": "ok" if healthy else "degraded",
            "kandinsky_api": "reachable" if healthy else "unreachable",
            "base_url": settings.kandinsky_base_url,
            "cached": cached,
            "active_tasks": len(ctx.manager.list_active()),
        }
        return JSONResponse(body, status_code=200 if healthy else 503)

    @mcp.custom_route("/files/{rel_path:path}", methods=["GET"])
    async def files(request: Request) -> Response:
        rel_path = request.path_params["rel_path"]
        if not verify_file(
            settings.signing_key,
            rel_path,
            request.query_params.get("exp", ""),
            request.query_params.get("sig", ""),
        ):
            return JSONResponse(
                {"error": "forbidden", "hint": "Ссылка без подписи или с истёкшим сроком"},
                status_code=403,
            )
        try:
            path = ctx.store.resolve(rel_path)
        except KandinskyError:
            return JSONResponse({"error": "not_found"}, status_code=404)
        if not path.is_file():
            return JSONResponse({"error": "not_found"}, status_code=404)
        return FileResponse(path, media_type=_guess_mime(path))

    @mcp.custom_route("/stats", methods=["GET"])
    async def stats(request: Request) -> Response:
        """Расход по маршрутам за текущий месяц.

        API не отдаёт остаток квоты: эндпоинта нет, а лимит виден только
        в тексте ошибки 429. Здесь — наши собственные счётчики по созданным
        задачам, то есть фактический расход через этот сервер.
        """
        return JSONResponse(build_usage(ctx))

    app = mcp.streamable_http_app(
        streamable_http_path=settings.mcp_path,
        max_request_body_size=settings.mcp_max_body_bytes,
        host=settings.mcp_host,
    )

    if settings.auth_enabled:
        app.add_middleware(ApiKeyMiddleware, api_key=settings.api_key)
    app.add_middleware(
        RateLimitMiddleware,
        limiter=RateLimiter(settings.rate_limit_per_minute),
    )
    return app


def build_usage(ctx: AppContext) -> dict[str, Any]:
    """Считает расход по маршрутам за текущий календарный месяц (UTC)."""
    month = time.strftime("%Y-%m", time.gmtime())
    month_start = calendar.timegm(time.strptime(month + "-01", "%Y-%m-%d"))

    routes: dict[str, dict[str, Any]] = {}
    for record in ctx.store.list_tasks(limit=10_000):
        if record.created_at < month_start:
            continue
        bucket = routes.setdefault(
            record.route,
            {"created": 0, "saved": 0, "failed": 0, "in_progress": 0, "last_error": None},
        )
        bucket["created"] += 1
        if record.status == "saved":
            bucket["saved"] += 1
        elif record.status == "failed":
            bucket["failed"] += 1
            if record.error:
                bucket["last_error"] = record.error[:200]
        elif not record.is_terminal:
            bucket["in_progress"] += 1

    # Подмешиваем последние известные сведения о квоте из ответов 429.
    for route, info in ctx.store.load_quota().items():
        bucket = routes.setdefault(
            route,
            {"created": 0, "saved": 0, "failed": 0, "in_progress": 0, "last_error": None},
        )
        used, limit = info.get("used"), info.get("limit")
        bucket["quota"] = {
            "type": info.get("type"),
            "used": used,
            "limit": limit,
            "remaining": max(0, limit - used) if isinstance(used, int) and isinstance(limit, int) else None,
            "checked_at": info.get("checked_at"),
        }

    return {
        "month": month,
        "routes": routes,
        "total_created": sum(b["created"] for b in routes.values()),
        "note": (
            "created/saved/failed — расход через этот сервер за текущий месяц. "
            "Остаток API отдельным методом не отдаёт, но в ответе 429 присылает "
            "used и limit — они попадают в поле quota (появляется после первого "
            "отказа по квоте). Отклонённые запросы задачу не создают и в created "
            "не считаются."
        ),
    }


def _guess_mime(path: Path) -> str:
    return {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".mp4": "video/mp4",
        ".wav": "audio/wav",
    }.get(path.suffix.lower(), "application/octet-stream")
