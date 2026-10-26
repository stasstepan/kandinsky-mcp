"""Инструменты генерации изображений.

Важное правило: ни одно исключение не должно вылетать из тула. MCP SDK
превращает необработанное исключение в неинформативное «Error executing tool»,
а модели нужно прочитать код и подсказку. Поэтому все доменные ошибки здесь
переводятся в `is_error`-результат.
"""

from __future__ import annotations

import asyncio
import time
from typing import Annotated, Any, Literal

from mcp.server import MCPServer
from mcp.types import CallToolResult
from pydantic import Field

from ..api.errors import KandinskyError
from ..api.routes import K5_IMAGE_T2I, K6_IMAGE_T2I, RouteSpec, normalize_params
from ..domain.media import prepare_image
from ..domain.store import TaskRecord
from .context import AppContext, error_result, result_with_images

WAIT_MAX = 300

# Фактический предел ожидания внутри одного вызова. Клиенты обычно обрывают
# запрос к инструменту на 60 секундах, и тогда агент видит «Timed out while
# waiting for response», хотя задача жива. Держимся ниже этого порога.
WAIT_EFFECTIVE_MAX = 45

# Значение по умолчанию берётся из настроек (DEFAULT_WAIT_SECONDS), чтобы его
# можно было менять без пересборки: см. register_image_tools.
WAIT_FALLBACK = 25


def clamp_wait(seconds: float) -> float:
    """Ограничивает ожидание разумным пределом.

    `wait_seconds` больше 45 бессмысленен: клиент всё равно оборвёт вызов
    раньше, а задача продолжит считаться — её заберут через task_result.
    """
    return min(max(0.0, float(seconds)), WAIT_EFFECTIVE_MAX)


async def wait_for_local_terminal(ctx: AppContext, task_id: str, seconds: float) -> TaskRecord:
    """Ждёт, пока фоновый поллер приведёт задачу к терминальному состоянию.

    Ждём именно локальное состояние, а не API: к этому моменту файл уже скачан,
    поэтому окно одноразовой выдачи нас не ограничивает.
    """
    deadline = time.monotonic() + max(0.0, seconds)
    while time.monotonic() < deadline:
        record = ctx.store.load_task(task_id)
        if record is not None and record.is_terminal:
            return record
        await asyncio.sleep(0.5)
    return ctx.store.require_task(task_id)


def inline_images(ctx: AppContext, record: TaskRecord) -> list[tuple[bytes, str]]:
    """Отбирает картинки для inline-превью по порогу размера."""
    if record.status != "saved":
        return []
    images: list[tuple[bytes, str]] = []
    for stored in record.files:
        if not stored.mime.startswith("image/"):
            continue
        if stored.bytes > ctx.settings.max_inline_bytes:
            continue
        try:
            images.append((ctx.store.resolve(stored.rel_path).read_bytes(), stored.mime))
        except (OSError, KandinskyError):
            continue
    return images


def build_payload(
    ctx: AppContext,
    record: TaskRecord,
    *,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    """Единый формат ответа: агент видит одно и то же во всех инструментах."""
    payload: dict[str, Any] = {
        "task_id": record.task_id,
        "status": record.status,
        "model": record.route,
        "prompt": record.prompt,
        "params": record.params,
        "elapsed_ms": record.duration_ms,
    }
    if record.files:
        payload["media"] = ctx.media_payload(record)
    if record.status == "saved":
        payload["next"] = "Файл сохранён. Ссылка media[].url отдаёт его по подписи."
    elif record.status == "failed":
        payload["error"] = record.error
        payload["next"] = (
            "Генерация не удалась. Смотрите error; повтор без изменений обычно бесполезен."
        )
    else:
        payload["next"] = (
            f"Генерация ещё идёт. Файл скачается фоновым поллером сам — повторять "
            f"генерацию не нужно. Опрашивайте не чаще раза в 30 секунд: "
            f"kandinsky_task_result(task_id={record.task_id}) отдаст готовый файл. "
            "Каждый вызов — отдельный запрос, частый опрос упирается в лимит."
        )
    if warnings:
        payload["warnings"] = warnings
    return payload


async def submit(
    ctx: AppContext,
    spec: RouteSpec,
    params: dict[str, Any],
    *,
    censor: bool | None,
    prompt: str,
    wait_seconds: float,
    warnings: list[str] | None = None,
    force_new: bool = False,
) -> CallToolResult:
    """Общий путь: валидация → создание задачи → ожидание → ответ."""
    collected = list(warnings or [])
    effective_wait = clamp_wait(wait_seconds)
    if wait_seconds > WAIT_EFFECTIVE_MAX:
        collected.append(
            f"Ждём не больше {WAIT_EFFECTIVE_MAX} с за вызов: клиенты обычно обрывают "
            "запрос к инструменту на 60 секундах, и тогда вы получите ошибку таймаута, "
            "хотя задача продолжится. Если результат не готов, вернётся task_id — "
            "заберите его позже через kandinsky_task_result."
        )
    try:
        cleaned = normalize_params(spec, params)
        effective_censor = ctx.settings.default_censor if censor is None else censor
        record, deduplicated = await ctx.manager.create_ex(
            spec.route, cleaned, censor=effective_censor, prompt=prompt, force_new=force_new
        )
        record = await wait_for_local_terminal(ctx, record.task_id, effective_wait)
    except KandinskyError as exc:
        return error_result(exc.code, exc.message, exc.hint)

    if deduplicated:
        collected.append(
            "Такой же запрос уже выполнялся меньше минуты назад — возвращена "
            "существующая задача, квота не потрачена. Нужна другая картинка: "
            "повторите с force_new=true."
        )

    payload = build_payload(ctx, record, warnings=collected or None)
    if record.status == "failed":
        return error_result(
            "generation_failed",
            record.error or "Генерация не удалась",
            payload["next"],
        )
    return result_with_images(payload, inline_images(ctx, record))


def _collect_warnings(
    spec: RouteSpec, requested: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    """Отбрасывает параметры, которых нет в маршруте, и объясняет это агенту."""
    kept: dict[str, Any] = {}
    warnings: list[str] = []
    for key, value in requested.items():
        if value is None:
            continue
        if key in spec.allowed:
            kept[key] = value
        else:
            warnings.append(
                f"Параметр {key} не поддерживается маршрутом {spec.route} и проигнорирован."
            )
    return kept, warnings


def _prepare_input(ctx: AppContext, source: str, spec: RouteSpec) -> tuple[str, list[str]]:
    """Готовит входную картинку: читает, приводит к лимитам, кодирует в base64."""
    raw = ctx.load_image(source)
    image = prepare_image(raw, max_mb=spec.image_max_mb, max_side=spec.image_max_side)
    notes = [f"Входная картинка: {image.describe()}"] if (image.resized or image.recompressed) else []
    return image.base64, notes


def register_image_tools(mcp: MCPServer, ctx: AppContext) -> None:
    wait_default = min(max(0, getattr(ctx.settings, "default_wait_seconds", WAIT_FALLBACK)), WAIT_MAX)

    @mcp.tool(
        name="kandinsky_image_generate",
        title="Генерация изображения по тексту",
        description=(
            "Создаёт изображение по текстовому описанию через Kandinsky API.\n"
            "Generates an image from a text prompt via Kandinsky API.\n\n"
            "Файл сохраняется на сервере, в ответ приходят подписанная ссылка и "
            "превью картинки. Ожидание внутри вызова ограничено wait_seconds: если "
            "не успело, вернётся task_id — результат всё равно будет скачан фоновым "
            "поллером, его можно забрать через kandinsky_task_result."
        ),
    )
    async def kandinsky_image_generate(
        prompt: Annotated[
            str, Field(description="Текстовый промпт, до 2048 символов. / Text prompt.")
        ],
        resolution: Annotated[
            str,
            Field(
                description=(
                    "Разрешение: 1024x1024, 768x768, 768x1280, 1280x768, 512x512 или auto. "
                    "Разделитель ':' тоже принимается. / Output resolution."
                )
            ),
        ] = "1024x1024",
        model: Annotated[
            Literal["k6", "k5"],
            Field(description="Модель: k6 (по умолчанию) или k5. / Model choice."),
        ] = "k6",
        beautificator: Annotated[
            Literal["enabled", "disabled", "gigachat-max"] | None,
            Field(description="Переписывание промпта через LLM (только k6). / Prompt beautification."),
        ] = None,
        censor: Annotated[
            bool | None,
            Field(description="Цензура контента. По умолчанию включена. / Content censor flag."),
        ] = None,
        guidance: Annotated[
            float | None,
            Field(description="Сила следования промпту, только k5. / Guidance scale (k5 only)."),
        ] = None,
        use_original_prompt: Annotated[
            bool | None,
            Field(description="Не расширять промпт через LLM, только k5. / Skip expansion (k5 only)."),
        ] = None,
        wait_seconds: Annotated[
            int,
            Field(
                ge=0,
                le=WAIT_MAX,
                description=(
                    "Сколько секунд ждать результата внутри вызова. 0 — только создать задачу."
                    " / Seconds to wait inside the call; 0 - just create the task."
                ),
            ),
        ] = wait_default,
        force_new: Annotated[
            bool,
            Field(
                description=(
                    "Создать новую задачу, даже если точно такой же запрос уже был "
                    "меньше минуты назад. По умолчанию повтор возвращает существующую "
                    "задачу и не тратит квоту. / Force a new task even if an identical "
                    "request just ran."
                )
            ),
        ] = False,
    ) -> CallToolResult:
        spec = K6_IMAGE_T2I if model == "k6" else K5_IMAGE_T2I
        params, warnings = _collect_warnings(
            spec,
            {
                "query": prompt,
                "resolution": resolution,
                "beautificator": beautificator,
                "guidance": guidance,
                "use_original_prompt": use_original_prompt,
            },
        )
        return await submit(
            ctx,
            spec,
            params,
            censor=censor,
            prompt=prompt,
            wait_seconds=wait_seconds,
            force_new=force_new,
            warnings=warnings,
        )

    @mcp.tool(
        name="kandinsky_image_edit",
        title="Редактирование изображения по тексту",
        description=(
            "Меняет существующее изображение по описанию (image-to-image).\n"
            "Edits an existing image from a text prompt (image-to-image).\n\n"
            "Принимает 1–3 изображения в base64, data-URI или (в stdio-режиме) путём "
            "к файлу. Слишком большие картинки уменьшаются автоматически."
        ),
    )
    async def kandinsky_image_edit(
        prompt: Annotated[
            str, Field(description="Что изменить, до 2048 символов. / Edit instruction.")
        ],
        images: Annotated[
            list[str],
            Field(
                min_length=1,
                description="1–3 изображения: base64, data-URI или путь. / Source images (1-3).",
            ),
        ],
        model: Annotated[
            Literal["k6", "kandishop"],
            Field(description="k6 — обычная модель, kandishop — квантованная. / Model variant."),
        ] = "k6",
        beautificator: Annotated[
            Literal["enabled", "disabled", "gigachat-max"] | None,
            Field(description="Переписывание промпта через LLM. / Prompt beautification."),
        ] = None,
        censor: Annotated[
            bool | None, Field(description="Цензура контента. / Content censor.")
        ] = None,
        wait_seconds: Annotated[
            int,
            Field(ge=0, le=WAIT_MAX, description="Секунд ожидания внутри вызова. / Seconds to wait."),
        ] = wait_default,
        force_new: Annotated[
            bool,
            Field(
                description=(
                    "Создать новую задачу, даже если точно такой же запрос уже был "
                    "меньше минуты назад. По умолчанию повтор возвращает существующую "
                    "задачу и не тратит квоту. / Force a new task even if an identical "
                    "request just ran."
                )
            ),
        ] = False,
    ) -> CallToolResult:
        route = "k6-i2i" if model == "k6" else "k6-kandishop-quantized"
        try:
            spec = ctx.registry.get(route)
        except KandinskyError as exc:
            return error_result(exc.code, exc.message, exc.hint)

        if len(images) > spec.max_images:
            return error_result(
                "validation_error",
                f"Маршрут {route} принимает не больше {spec.max_images} изображений, "
                f"передано {len(images)}.",
                "Уберите лишние изображения и повторите.",
            )

        prepared: list[str] = []
        notes: list[str] = []
        for index, source in enumerate(images, start=1):
            try:
                encoded, item_notes = _prepare_input(ctx, source, spec)
            except ValueError as exc:
                return error_result("validation_error", f"Изображение {index}: {exc}")
            except KandinskyError as exc:
                return error_result(exc.code, f"Изображение {index}: {exc.message}", exc.hint)
            prepared.append(encoded)
            notes.extend(item_notes)

        params, warnings = _collect_warnings(
            spec, {"query": prompt, "image": prepared, "beautificator": beautificator}
        )
        return await submit(
            ctx,
            spec,
            params,
            censor=censor,
            prompt=prompt,
            wait_seconds=wait_seconds,
            force_new=force_new,
            warnings=notes + warnings,
        )

    @mcp.tool(
        name="kandinsky_image_upscale",
        title="Увеличение разрешения изображения",
        description=(
            "Увеличивает изображение в 2 или 4 раза (super resolution).\n"
            "Upscales an image 2x or 4x (super resolution).\n\n"
            "Вход строже, чем у остальных маршрутов: до 4 МБ и 1024 px по стороне — "
            "сервер приведёт картинку к лимиту сам."
        ),
    )
    async def kandinsky_image_upscale(
        image: Annotated[
            str, Field(description="Изображение: base64, data-URI или путь. / Source image.")
        ],
        upscale: Annotated[
            Literal[2, 4], Field(description="Множитель увеличения: 2 или 4. / Upscale factor.")
        ] = 2,
        one_step_t: Annotated[
            float | None,
            Field(ge=0.0, le=1.0, description="Резкость 0..1. / Sharpness 0..1."),
        ] = None,
        censor: Annotated[
            bool | None, Field(description="Цензура контента. / Content censor.")
        ] = None,
        wait_seconds: Annotated[
            int,
            Field(ge=0, le=WAIT_MAX, description="Секунд ожидания внутри вызова. / Seconds to wait."),
        ] = wait_default,
        force_new: Annotated[
            bool,
            Field(
                description=(
                    "Создать новую задачу, даже если точно такой же запрос уже был "
                    "меньше минуты назад. По умолчанию повтор возвращает существующую "
                    "задачу и не тратит квоту. / Force a new task even if an identical "
                    "request just ran."
                )
            ),
        ] = False,
    ) -> CallToolResult:
        try:
            spec = ctx.registry.get("k6_superres")
            encoded, notes = _prepare_input(ctx, image, spec)
        except ValueError as exc:
            return error_result("validation_error", str(exc))
        except KandinskyError as exc:
            return error_result(exc.code, exc.message, exc.hint)

        params, warnings = _collect_warnings(
            spec,
            {"image": encoded, "upscale": int(upscale), "one_step_t": one_step_t},
        )
        return await submit(
            ctx,
            spec,
            params,
            censor=censor,
            prompt=f"upscale x{upscale}",
            wait_seconds=wait_seconds,
            force_new=force_new,
            warnings=notes + warnings,
        )
