"""Сборка приложения: конфиг → клиент → реестр → задачи → MCP-сервер."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass

from mcp.server import MCPServer
from starlette.applications import Starlette

from . import __version__
from .api.client import KandinskyClient
from .api.routes import RouteRegistry
from .config import Settings
from .domain.store import FileStore
from .domain.tasks import TaskManager
from .tools import register_all
from .tools.context import AppContext

log = logging.getLogger("kandinsky_mcp")

GC_INTERVAL_SECONDS = 3600.0

INSTRUCTIONS = """\
Сервер генерации изображений Kandinsky.

Порядок работы:
1. Если не уверены в параметрах — вызовите kandinsky_routes: там модели,
   допустимые разрешения и лимиты именно этого хоста API.
2. Генерация: kandinsky_image_generate, kandinsky_image_edit,
   kandinsky_image_upscale. Каждый вызов создаёт задачу и ждёт результат
   внутри вызова (wait_seconds), возвращая ссылку на файл и превью картинки.
3. Если вызов вернул status=queued/processing — генерация ещё идёт.
   Файл скачивается фоновым поллером сам: kandinsky_task_status покажет
   состояние, kandinsky_task_result отдаст готовый файл без повторного
   обращения к API.
4. Ошибка с кодом censored значит, что контент заблокирован. Повтор того же
   промпта бесполезен — перепишите запрос.
"""


@dataclass(slots=True)
class ServerBundle:
    settings: Settings
    ctx: AppContext
    mcp: MCPServer
    app: Starlette


def build(settings: Settings | None = None, sync_routes: bool | None = None) -> ServerBundle:
    """Собирает всё, что нужно для запуска. Синхронно — сеть трогается только в lifespan."""
    settings = settings or Settings()

    if settings.mcp_transport == "http":
        settings.validate_for_http()
    else:
        settings.require_token()

    # В stdio-режиме сервер и агент живут на одной машине — можно читать файлы.
    allow_local_paths = settings.allow_local_paths or settings.mcp_transport == "stdio"

    store = FileStore(settings.data_dir)
    registry = RouteRegistry()
    client = KandinskyClient(
        settings.kandinsky_base_url,
        settings.token,
        timeout=settings.http_timeout,
        download_timeout=settings.http_download_timeout,
        max_attempts=settings.http_max_attempts,
        user_agent=f"kandinsky-mcp/{__version__}",
    )
    manager = TaskManager(
        client,
        store,
        registry,
        poll_min=settings.poll_min_seconds,
        poll_max=settings.poll_max_seconds,
        concurrency=settings.poll_concurrency,
        dedupe_window=settings.dedupe_window_seconds,
    )
    ctx = AppContext(
        settings=settings,
        client=client,
        store=store,
        registry=registry,
        manager=manager,
        allow_local_paths=allow_local_paths,
    )

    do_sync = settings.route_sync if sync_routes is None else sync_routes

    @contextlib.asynccontextmanager
    async def lifespan(server: MCPServer) -> AsyncIterator[dict[str, object]]:
        if do_sync:
            await _sync_routes(ctx)
        resumed = await manager.resume()
        if resumed:
            log.info("возобновлено задач после рестарта: %s", resumed)

        gc_task = asyncio.create_task(_gc_loop(ctx))
        try:
            yield {"ctx": ctx}
        finally:
            gc_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await gc_task
            await manager.close()
            await client.close()

    mcp = MCPServer(
        name="kandinsky-mcp",
        title="Kandinsky MCP",
        description="Генерация изображений через Kandinsky API",
        instructions=INSTRUCTIONS,
        version=__version__,
        lifespan=lifespan,
    )
    register_all(mcp, ctx)

    from .http_app import build_app  # поздний импорт: нужен уже настроенный mcp

    app = build_app(mcp, ctx)
    return ServerBundle(settings=settings, ctx=ctx, mcp=mcp, app=app)


async def _sync_routes(ctx: AppContext) -> None:
    """Сверяет реестр с живой OpenAPI-спекой. Не роняет сервер при сбое."""
    try:
        spec = await ctx.client.fetch_openapi()
    except Exception as exc:
        log.warning("не удалось получить openapi.json (%s), работаем по встроенному реестру", exc)
        return

    report = ctx.registry.sync_with_openapi(spec)
    if report.unavailable:
        log.warning("маршруты отсутствуют на хосте: %s", ", ".join(report.unavailable))
    if report.unknown:
        log.info("на хосте появились неизвестные маршруты: %s", ", ".join(report.unknown))
    for mismatch in report.enum_mismatches:
        log.warning("расхождение с API: %s", mismatch)
    if report.clean:
        log.info("реестр маршрутов совпадает с API")


async def _gc_loop(ctx: AppContext) -> None:
    while True:
        await asyncio.sleep(GC_INTERVAL_SECONDS)
        try:
            removed = ctx.store.gc(ctx.settings.result_ttl_hours)
            if removed:
                log.info("удалено просроченных задач: %s", removed)
        except Exception:
            log.exception("ошибка при уборке результатов")


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=level,
        format='{"ts":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}',
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
