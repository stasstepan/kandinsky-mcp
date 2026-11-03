"""Инструменты работы с задачами: статус, результат, список."""

from __future__ import annotations

import base64
from typing import Annotated, Any, Literal

from mcp.server import MCPServer
from mcp.types import CallToolResult
from pydantic import Field

from ..api.errors import NOT_READY, KandinskyError
from ..domain.store import TaskRecord
from ..profile import read_only_annotations, result_file_annotations
from .context import AppContext, error_result, result_with_images, text_result
from .image import build_payload, inline_images

# Предел на выдачу файла в base64. Картинка 1024x1024 в JPEG — 150-300 КБ,
# то есть base64 ~1.37x от этого; 4 МБ оставляют большой запас, но отсекают
# 4x-апскейл и видео, которые в JSON-ответ не влезут.
MAX_BASE64_BYTES = 4 * 1024 * 1024


def attach_base64(ctx: AppContext, record: TaskRecord, payload: dict[str, Any]) -> dict[str, Any]:
    """Добавляет media[].b64, читая файлы с локального диска.

    В API не ходит: работает с уже скачанными файлами, поэтому повторные вызовы
    безопасны и не тратят квоту.
    """
    media = payload.get("media") or []
    encoded = 0
    too_large = False

    for item, stored in zip(media, record.files, strict=False):
        if stored.bytes > MAX_BASE64_BYTES:
            item["b64_error"] = "file_too_large"
            too_large = True
            continue
        try:
            raw = ctx.store.resolve(stored.rel_path).read_bytes()
        except (OSError, KandinskyError):
            item["b64_error"] = "file_missing"
            continue
        item["b64"] = base64.b64encode(raw).decode("ascii")
        encoded += 1

    if too_large:
        payload["next"] = (
            "Файл больше 4 МБ, в base64 не отдаётся — скачайте по media[].url. "
            "Остальные поля ответа в силе."
        )
    elif encoded:
        payload["next"] = "Файл сохранён. Поле media[].b64 содержит данные файла."
    return payload


def register_task_tools(mcp: MCPServer, ctx: AppContext) -> None:
    @mcp.tool(
        name="kandinsky_task_status",
        title="Статус задачи генерации",
        annotations=read_only_annotations(),
        description=(
            "Возвращает состояние задачи: queued, processing, saved или failed.\n"
            "Returns task state: queued, processing, saved or failed.\n\n"
            "Только читает: новых задач не создаёт и квоту не тратит. Состояние "
            "локальное: файл уже на сервере, если статус saved. refresh=true "
            "дополнительно спрашивает API.\n\n"
            "Когда не использовать: чтобы получить сам файл — kandinsky_task_result."
        ),
    )
    async def kandinsky_task_status(
        task_id: Annotated[str, Field(description="Идентификатор задачи. / Task id.")],
        refresh: Annotated[
            bool,
            Field(
                description=(
                    "Спросить API сейчас, а не отдавать кеш. При статусе done "
                    "результат сразу скачивается. / Poll the API immediately."
                )
            ),
        ] = False,
    ) -> CallToolResult:
        try:
            record = await ctx.manager.refresh(task_id) if refresh else await ctx.manager.get(task_id)
        except KandinskyError as exc:
            return error_result(exc.code, exc.message, exc.hint)

        payload = build_payload(ctx, record)
        payload["active_pollers"] = len(ctx.manager.list_active())
        return result_with_images(payload, (), max_chars=ctx.settings.max_inline_result_chars)

    @mcp.tool(
        name="kandinsky_task_result",
        title="Результат задачи",
        annotations=result_file_annotations(),
        description=(
            "Отдаёт готовый файл: метаданные, ссылку на скачивание и превью.\n"
            "Returns the finished file: metadata, download URL and an inline preview.\n\n"
            "Только читает: в API не ходит, файл уже скачан фоновым поллером, поэтому "
            "повторные вызовы безопасны и квоту не тратят. В structuredContent — "
            "filename, content_type, download_url, size; файл добавляется в сессию "
            "GigaCowork.\n\n"
            "Когда не использовать: пока задача не готова (вернётся isError=not_ready) — "
            "дождитесь kandinsky_task_status. Не вызывайте для повторной генерации."
        ),
    )
    async def kandinsky_task_result(
        task_id: Annotated[str, Field(description="Идентификатор задачи. / Task id.")],
        inline: Annotated[
            bool, Field(description="Встраивать превью картинки в ответ. / Attach image preview.")
        ] = True,
        return_base64: Annotated[
            bool,
            Field(
                description=(
                    "Вернуть содержимое файла в поле media[].b64 — для агентов, которые "
                    "не могут скачать файл по ссылке. Файлы больше 4 МБ не кодируются: "
                    "в элементе будет b64_error=file_too_large. / Return file bytes as "
                    "base64 in media[].b64 (files over 4 MB are not encoded)."
                )
            ),
        ] = False,
    ) -> CallToolResult:
        try:
            record = await ctx.manager.get(task_id)
        except KandinskyError as exc:
            return error_result(exc.code, exc.message, exc.hint)

        if record.status == "failed":
            return error_result(
                "generation_failed", record.error or "Задача завершилась ошибкой", build_payload(ctx, record)["next"]
            )
        if record.status != "saved":
            return error_result(
                NOT_READY,
                f"Задача ещё не готова: status={record.status}",
                "Дождитесь завершения через kandinsky_task_status или повторите позже.",
            )

        payload = build_payload(ctx, record)
        if return_base64:
            payload = attach_base64(ctx, record, payload)
        return result_with_images(
            payload,
            inline_images(ctx, record) if inline else [],
            max_chars=ctx.settings.max_inline_result_chars,
        )

    @mcp.tool(
        name="kandinsky_tasks_list",
        title="Список задач",
        annotations=read_only_annotations(),
        description=(
            "Последние задачи с фильтрами по статусу и модели.\n"
            "Lists recent tasks, optionally filtered by status or model.\n\n"
            "Только читает локальный список задач; квоту не тратит. Промпты "
            "обрезаются до 120 символов.\n\n"
            "Когда не использовать: для файла конкретной задачи — "
            "kandinsky_task_result, для статуса одной задачи — kandinsky_task_status."
        ),
    )
    async def kandinsky_tasks_list(
        limit: Annotated[int, Field(ge=1, le=100, description="Сколько вернуть. / Max results.")] = 20,
        status: Annotated[
            Literal["queued", "processing", "saved", "failed", "cancelled"] | None,
            Field(description="Фильтр по статусу. / Filter by status."),
        ] = None,
        model: Annotated[
            str | None, Field(description="Фильтр по маршруту, например k6-image-t2i. / Filter by route.")
        ] = None,
    ) -> CallToolResult:
        records = ctx.store.list_tasks(limit=limit, status=status, route=model)
        payload = {
            "count": len(records),
            "tasks": [
                {
                    "task_id": r.task_id,
                    "status": r.status,
                    "model": r.route,
                    "prompt": (r.prompt[:120] + "…") if len(r.prompt) > 120 else r.prompt,
                    "elapsed_ms": r.duration_ms,
                    "files": len(r.files),
                    "error": r.error,
                }
                for r in records
            ],
        }
        return text_result(payload)
