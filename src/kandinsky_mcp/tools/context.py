"""Общий контекст тулов и помощники для ответов.

`AppContext` собирается один раз при старте и передаётся в тулы через замыкание:
так не нужны глобальные переменные, а тесты могут подставить свой контекст.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcp.types import CallToolResult, ImageContent, TextContent

from ..api.client import KandinskyClient
from ..api.errors import KandinskyError
from ..api.routes import RouteRegistry
from ..auth import sign_file
from ..config import Settings
from ..domain.store import FileStore, TaskRecord
from ..domain.tasks import TaskManager


@dataclass(slots=True)
class AppContext:
    settings: Settings
    client: KandinskyClient
    store: FileStore
    registry: RouteRegistry
    manager: TaskManager
    allow_local_paths: bool = False

    # --- файлы ---------------------------------------------------------

    def file_url(self, rel_path: str) -> str:
        query, _expires = sign_file(self.settings.signing_key, rel_path, self.settings.file_url_ttl_seconds)
        return f"{self.settings.public_base_url}/files/{rel_path}?{query}"

    def media_payload(self, record: TaskRecord) -> list[dict[str, Any]]:
        return [
            {
                "filename": Path(f.rel_path).name,
                "mime": f.mime,
                "bytes": f.bytes,
                "sha256": f.sha256,
                "url": self.file_url(f.rel_path),
                "path": f.rel_path,
            }
            for f in record.files
        ]

    # --- входные изображения -------------------------------------------

    def load_image(self, value: str) -> bytes:
        """Принимает data-URI, чистый base64 или локальный путь.

        Локальные пути разрешены только в stdio-режиме: в HTTP-режиме это была бы
        возможность прочитать произвольный файл сервера.
        """
        text = str(value).strip()
        if not text:
            raise ValueError("Пустой источник изображения")

        if text.startswith("data:"):
            _, _, payload = text.partition(",")
            try:
                return base64.b64decode(payload, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ValueError(f"Некорректный data-URI: {exc}") from exc

        # Своё хранилище: `results/<дата>/<id>.jpg` или наша же подписанная
        # ссылка на файл. Читаем с диска, без сети — это дешевле, чем гонять
        # сотни килобайт base64 через аргументы инструмента.
        known = self._from_own_storage(text)
        if known is not None:
            return known

        path = Path(text)
        if self.allow_local_paths and path.exists() and path.is_file():
            return path.read_bytes()

        # Похоже на base64: длинная строка без пробелов и разделителей пути.
        if len(text) > 64 and " " not in text and not text.startswith(("/", "./", "..")):
            try:
                return base64.b64decode(text, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ValueError(
                    "Строка не похожа ни на base64, ни на существующий файл. "
                    "Передайте изображение как base64 или data-URI."
                ) from exc

        raise ValueError(
            f"Файл {text!r} недоступен серверу: он работает на другой машине и не "
            "видит файлы сессии агента. Прочитайте файл у себя и передайте "
            "содержимое: base64.b64encode(open(path, 'rb').read()).decode() — "
            "или готовый data:image/jpeg;base64,… . Если картинка была получена "
            "от этого сервера, копию можно не гонять: передайте её path из ответа "
            "(results/<дата>/<id>.jpg) или ссылку media[].url."
        )

    def _from_own_storage(self, text: str) -> bytes | None:
        """Пытается прочитать файл из собственного хранилища по пути или ссылке."""
        candidate: str | None = None

        if text.startswith(("results/", "/results/")):
            candidate = text.lstrip("/")
        else:
            prefix = f"{self.settings.public_base_url}/files/"
            if text.startswith(prefix):
                candidate = text[len(prefix) :].split("?", 1)[0]

        if not candidate:
            return None
        try:
            path = self.store.resolve(candidate)
        except (KandinskyError, OSError):
            return None
        return path.read_bytes() if path.is_file() else None


# --- сборка ответов ------------------------------------------------------------


def as_json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


# Сколько символов в результате инструмента готовы принять «обычные» клиенты.
# GigaCowork обрезает результат длиннее 100 000 символов, поэтому inline-превью
# добавляем только пока ответ укладывается в бюджет.
INLINE_RESULT_BUDGET = 90_000


def summary_text(data: dict[str, Any]) -> str:
    """Короткая сводка без URL, путей и base64.

    Текст читает модель, поэтому временные ссылки на файлы в него не попадают:
    они живут в structuredContent (``download_url``) и в MCP image-блоке.
    """
    status = data.get("status")
    task_id = data.get("task_id")
    head = f"task_id={task_id}, status={status}, model={data.get('model')}"

    lines: list[str] = []
    if status == "saved":
        for item in data.get("media") or []:
            lines.append(
                f"{head}; файл: {item.get('filename') or 'результат'}, "
                f"{item.get('mime')}, {item.get('bytes')} Б, "
                f"sha256={str(item.get('sha256') or '')[:16]}…"
            )
        if not data.get("media"):
            lines.append(f"{head}; файл готов")
    elif status == "failed":
        lines.append(f"{head}; ошибка: {data.get('error')}")
    else:
        lines.append(
            f"{head}; генерация ещё идёт — заберите результат через "
            f"kandinsky_task_result(task_id={task_id})"
        )

    next_step = data.get("next")
    if next_step:
        lines.append(str(next_step))
    for warning in data.get("warnings") or []:
        lines.append(f"warning: {warning}")
    return "\n".join(lines)


def text_result(data: dict[str, Any]) -> CallToolResult:
    """Ответ без медиа: текст + структурированные данные."""
    return CallToolResult(
        content=[TextContent(type="text", text=as_json(data))],
        structured_content=data,
    )


def result_with_images(
    data: dict[str, Any],
    images: Sequence[tuple[bytes, str]] = (),
    *,
    summary: str | None = None,
    max_chars: int | None = None,
) -> CallToolResult:
    """Ответ с inline-превью: краткий текст, структурированные данные, картинки.

    Превью добавляется, только пока результат укладывается в ``max_chars``:
    GigaCowork обрезает ответы длиннее 100 000 символов, и обрезанный base64
    ломает картинку. Полный файл всегда доступен через structuredContent.
    """
    text = summary if summary is not None else summary_text(data)
    blocks: list[Any] = [TextContent(type="text", text=text)]

    budget = (INLINE_RESULT_BUDGET if max_chars is None else max_chars) - len(text) - 500
    for raw, mime in images:
        estimated = ((len(raw) + 2) // 3) * 4
        if estimated > budget:
            continue
        budget -= estimated
        blocks.append(
            ImageContent(
                type="image",
                data=base64.b64encode(raw).decode("ascii"),
                mime_type=mime,
            )
        )
    return CallToolResult(content=blocks, structured_content=data)


def error_result(
    code: str, message: str, hint: str = "", extra: dict[str, Any] | None = None
) -> CallToolResult:
    """Ошибка, которую модель прочитает и сможет обработать (is_error=True)."""
    payload: dict[str, Any] = {"code": code, "message": message, "hint": hint}
    if extra:
        payload.update(extra)
    text = f"[{code}] {message}" + (f"\n{hint}" if hint else "")
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        structured_content=payload,
        is_error=True,
    )
