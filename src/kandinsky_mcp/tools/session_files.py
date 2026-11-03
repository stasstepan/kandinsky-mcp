"""Входной файл сессии GigaCowork (``x-session-file-input``).

Платформа подставляет в технический аргумент ``read_url`` временную HTTPS-ссылку
на файл из сессии. Сервер обязан:

- скачать файл методом GET в пределах одного вызова;
- не логировать и не сохранять URL, не возвращать его в ответе и ошибке;
- уложиться в лимит размера и таймаут.

Ссылка — адресное разрешение на один файл, поэтому в сообщениях об ошибках
всегда только нейтральный текст.
"""

from __future__ import annotations

from urllib.parse import urlsplit

import httpx

from ..api.errors import VALIDATION_ERROR, KandinskyError


def _fail(message: str, hint: str) -> KandinskyError:
    return KandinskyError(VALIDATION_ERROR, message, hint=hint)


async def download_read_url(url: str, *, max_bytes: int, timeout: float) -> bytes:
    """Скачивает входной файл сессии по временному read_url.

    Возвращает сырые байты; подготовку (лимиты, пережатие) делает вызывающий.
    Все ошибки — доменные, без URL в тексте.
    """
    text = str(url or "").strip()
    parsed = urlsplit(text)
    if parsed.scheme != "https" or not parsed.netloc:
        raise _fail(
            "Входной файл сессии должен быть HTTPS-ссылкой",
            "Передайте файл через выбор файла в GigaCowork; вручную read_url не заполняется.",
        )

    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            response = await client.get(text)
    except httpx.HTTPError:
        raise _fail(
            "Не удалось скачать входной файл из сессии",
            "Файл мог быть удалён или ссылка истекла. Выберите файл ещё раз.",
        ) from None

    if response.status_code != 200:
        raise _fail(
            "Сервис сессии не отдал входной файл",
            f"Ответ {response.status_code}. Выберите файл ещё раз.",
        )
    body = response.content
    if not body:
        raise _fail("Входной файл пустой", "Выберите другой файл.")
    if len(body) > max_bytes:
        raise _fail(
            "Входной файл слишком большой",
            f"Лимит — {max_bytes // (1024 * 1024)} МБ. Уменьшите файл и повторите.",
        )
    return body
