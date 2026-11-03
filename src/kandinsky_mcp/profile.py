"""Профиль совместимости GigaCowork поверх MCP SDK.

Отличия от «голого» MCP, которые требует руководство GigaCowork:

1. расширение ``x-session-file-output`` в аннотациях инструментов — по нему
   платформа понимает, что файл из ``structuredContent.download_url`` нужно
   перенести в сессию;
2. ``outputSchema`` у каждого инструмента — чтобы ``structuredContent`` был
   описан и проходил проверку при регистрации;
3. JSON-RPC ``-32602`` на неизвестный инструмент и невалидные аргументы:
   это протокольные ошибки, а не прикладной ``isError``.

Расширения аннотаций: ``ToolAnnotations`` в ``mcp_types`` объявлена с
``extra="ignore"``, поэтому незнакомый ключ молча отбрасывается при сборке
ответа ``tools/list``. Разрешаем дополнительные поля до регистрации
инструментов — иначе GigaCowork не увидит маркер файлового режима.
"""

from __future__ import annotations

import logging
from typing import Any

from mcp.server import MCPServer
from mcp.shared.exceptions import MCPError
from mcp_types import ToolAnnotations
from pydantic import ValidationError

log = logging.getLogger("kandinsky_mcp.profile")

INVALID_PARAMS = -32602

# Минимальная схема успешного structuredContent: обязательны только поля,
# которые есть всегда. Остальное — дополнительно, чтобы не ломать payload.
_TASK_PAYLOAD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": True,
    "properties": {
        "task_id": {"type": "string"},
        "status": {"type": "string"},
        "model": {"type": "string"},
        "elapsed_ms": {"type": "integer"},
        "filename": {"type": "string"},
        "content_type": {"type": "string"},
        "download_url": {"type": "string"},
        "size": {"type": "integer"},
    },
    "required": ["task_id", "status"],
}

OUTPUT_SCHEMAS: dict[str, dict[str, Any]] = {
    "kandinsky_image_generate": _TASK_PAYLOAD_SCHEMA,
    "kandinsky_image_edit": _TASK_PAYLOAD_SCHEMA,
    "kandinsky_image_upscale": _TASK_PAYLOAD_SCHEMA,
    "kandinsky_task_status": _TASK_PAYLOAD_SCHEMA,
    "kandinsky_task_result": _TASK_PAYLOAD_SCHEMA,
    "kandinsky_tasks_list": {
        "type": "object",
        "additionalProperties": True,
        "properties": {
            "count": {"type": "integer"},
            "tasks": {"type": "array", "items": {"type": "object"}},
        },
        "required": ["count", "tasks"],
    },
    "kandinsky_routes": {
        "type": "object",
        "additionalProperties": True,
        "properties": {
            "count": {"type": "integer"},
            "routes": {"type": "array", "items": {"type": "object"}},
        },
        "required": ["count", "routes"],
    },
}


# Инструменты, у которых есть выходной файл: GigaCowork переносит его в сессию,
# только увидев маркер в аннотациях ответа tools/list.
FILE_OUTPUT_TOOLS = frozenset(
    {
        "kandinsky_image_generate",
        "kandinsky_image_edit",
        "kandinsky_image_upscale",
        "kandinsky_task_result",
    }
)


def _enable_annotation_extensions() -> None:
    """Разрешает незнакомые ключи в ToolAnnotations (нужно для x-session-file-*)."""
    try:
        if ToolAnnotations.model_config.get("extra") == "allow":
            return
        ToolAnnotations.model_config["extra"] = "allow"
        ToolAnnotations.model_rebuild(force=True)
    except Exception:  # pragma: no cover — SDK обновится, деградируем аккуратно
        log.warning("не удалось включить расширения ToolAnnotations", exc_info=True)


# Побочный эффект на импорте: инструменты создают аннотации при регистрации,
# а она происходит уже после импорта этого модуля.
_enable_annotation_extensions()


def _annotations(**fields: Any) -> ToolAnnotations:
    return ToolAnnotations.model_validate(fields)


def read_only_annotations() -> ToolAnnotations:
    """Инструмент только читает; внешнюю систему не меняет."""
    return _annotations(readOnlyHint=True, openWorldHint=False)


def generation_annotations() -> ToolAnnotations:
    """Создаёт платную задачу генерации и возвращает файл в сессию (url-режим)."""
    return _annotations(
        readOnlyHint=False,
        destructiveHint=False,
        openWorldHint=True,
        **{"x-session-file-output": "url"},
    )


def result_file_annotations() -> ToolAnnotations:
    """Отдаёт уже готовый файл; без аннотации GigaCowork не перенесёт его в сессию."""
    return _annotations(
        readOnlyHint=True,
        destructiveHint=False,
        openWorldHint=False,
        **{"x-session-file-output": "url"},
    )


def install(mcp: MCPServer) -> None:
    """Доукомплектовывает зарегистрированные инструменты под профиль GigaCowork."""
    _install_output_schemas(mcp)
    _install_wire_annotations()
    _install_call_tool_guard(mcp)


def _install_wire_annotations() -> None:
    """Возвращает x-session-file-output в JSON ответа tools/list.

    Даже с extra="allow" вложенная модель аннотаций сериализуется по схеме
    поля ``Tool.annotations``, которая собирается при импорте SDK и отбрасывает
    незнакомые ключи. Поэтому досыпаем маркер на уровне готового JSON — это
    единственное место, которое видит GigaCowork.
    """
    from mcp.server import runner as mcp_runner  # поздний импорт: SDK уже загружен

    if getattr(mcp_runner._dump_result, "_kandinsky_annotations", False):
        return
    original = mcp_runner._dump_result

    def dump_with_annotations(result: Any) -> dict[str, Any]:
        data = original(result)
        tools = data.get("tools") if isinstance(data, dict) else None
        if isinstance(tools, list):
            for tool in tools:
                if not isinstance(tool, dict) or tool.get("name") not in FILE_OUTPUT_TOOLS:
                    continue
                annotations = tool.get("annotations")
                if not isinstance(annotations, dict):
                    annotations = {}
                annotations["x-session-file-output"] = "url"
                tool["annotations"] = annotations
        return data

    dump_with_annotations._kandinsky_annotations = True  # type: ignore[attr-defined]
    mcp_runner._dump_result = dump_with_annotations


def _install_output_schemas(mcp: MCPServer) -> None:
    # Внутренний Tool хранит схему в fn_metadata; публичный API SDK не даёт
    # объявить outputSchema без модели результата, а мы возвращаем CallToolResult
    # напрямую. Схема нужна только на проводе, валидацию делает сама модель.
    for tool in mcp._tool_manager.list_tools():
        schema = OUTPUT_SCHEMAS.get(tool.name)
        if schema is not None and tool.fn_metadata.output_schema is None:
            tool.fn_metadata.output_schema = dict(schema)


def _install_call_tool_guard(mcp: MCPServer) -> None:
    """Превращает протокольные ошибки tools/call в JSON-RPC -32602.

    SDK 2.x по умолчанию заворачивает «Unknown tool» и ValidationError в
    ``result.isError=true``; профиль GigaCowork ожидает разные уровни:
    -32602 для структурно неверного вызова и isError для прикладной ошибки.
    """
    low = mcp._lowlevel_server
    entry = low.get_request_handler("tools/call")
    if entry is None or getattr(entry.handler, "_kandinsky_profile_guard", False):
        return
    original = entry.handler

    async def guarded(ctx: Any, params: Any) -> Any:
        name = getattr(params, "name", "")
        tool = mcp._tool_manager.get_tool(name)
        if tool is None:
            raise MCPError(INVALID_PARAMS, f"Unknown tool: {name}")
        try:
            tool.fn_metadata.validate_arguments(params.arguments or {})
        except ValidationError as exc:
            raise MCPError(INVALID_PARAMS, _invalid_arguments_message(exc)) from None
        return await original(ctx, params)

    guarded._kandinsky_profile_guard = True
    low.add_request_handler("tools/call", entry.params_type, guarded)


def _invalid_arguments_message(exc: ValidationError) -> str:
    """Короткое сообщение без значений аргументов и ссылок на pydantic."""
    details: list[str] = []
    for error in exc.errors():
        field = ".".join(str(part) for part in error.get("loc", ()) if part != "body")
        field = field or "arguments"
        kind = str(error.get("type", "invalid"))
        if kind == "missing":
            details.append(f"{field} is required")
        elif kind == "literal_error":
            details.append(f"{field} has an unsupported value")
        else:
            details.append(f"{field} is invalid ({kind})")
    return "Invalid tool arguments: " + "; ".join(details[:5])
