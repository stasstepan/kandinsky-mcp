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
) -> CallToolResult:
    """Ответ с inline-превью: текст, структурированные данные, картинки."""
    blocks: list[Any] = [TextContent(type="text", text=summary or as_json(data))]
    for raw, mime in images:
        blocks.append(
            ImageContent(
                type="image",
                data=base64.b64encode(raw).decode("ascii"),
                mime_type=mime,
            )
        )
    return CallToolResult(content=blocks, structured_content=data)


def error_result(code: str, message: str, hint: str = "") -> CallToolResult:
    """Ошибка, которую модель прочитает и сможет обработать (is_error=True)."""
    payload = {"code": code, "message": message, "hint": hint}
    text = f"[{code}] {message}" + (f"\n{hint}" if hint else "")
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        structured_content=payload,
        is_error=True,
    )
