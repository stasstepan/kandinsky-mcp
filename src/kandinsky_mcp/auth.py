"""Доступ к HTTP-эндпоинтам: ключ для MCP, подпись для файлов.

Почему middleware написан на чистом ASGI, а не через BaseHTTPMiddleware:
обёртка на BaseHTTPMiddleware буферизует поток и ломает SSE-ответы
Streamable HTTP. MCP-клиент в таком случае «висит» без событий.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from collections.abc import Awaitable, Callable
from typing import Any

from starlette.responses import JSONResponse

Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]

# Эти пути не требуют X-API-Key: health-проверки публичны по смыслу,
# а /files защищён собственной подписью в query-параметрах.
EXEMPT_PREFIXES = ("/healthz", "/readyz", "/files/")


def _header(scope: Scope, name: bytes) -> str:
    for key, value in scope.get("headers") or []:
        if key.lower() == name:
            return value.decode("latin-1")
    return ""


# Служебные заголовки, которые не могут нести ключ: их значения формирует
# инфраструктура, и совпадение с ключом было бы случайным.
_IGNORED_HEADERS = frozenset(
    {
        b"host",
        b"content-length",
        b"content-type",
        b"accept",
        b"accept-encoding",
        b"accept-language",
        b"connection",
        b"user-agent",
        b"cookie",
        b"referer",
        b"origin",
    }
)


def _candidates(value: str) -> tuple[str, ...]:
    """Варианты записи одного и того же ключа, которые считаем равными.

    Некоторые платформы обрезают префикс `sk-` или добавляют `Bearer`.
    """
    raw = value.strip()
    variants = [raw]
    if raw.lower().startswith("bearer "):
        variants.append(raw[7:].strip())
    for item in list(variants):
        if item.startswith("sk-"):
            variants.append(item[3:])
    return tuple(variants)


def _matches(provided: str, api_key: str) -> bool:
    if not provided:
        return False
    expected = {api_key}
    if api_key.startswith("sk-"):
        expected.add(api_key[3:])
    return any(
        hmac.compare_digest(candidate, exp)
        for candidate in _candidates(provided)
        for exp in expected
    )


class ApiKeyMiddleware:
    def __init__(self, app: Any, *, api_key: str, exempt: tuple[str, ...] = EXEMPT_PREFIXES) -> None:
        self.app = app
        self.api_key = api_key
        self.exempt = exempt

    def _authorized(self, scope: Scope) -> bool:
        """Проверяет ключ во всех разумных местах.

        Помимо штатных `X-API-Key` и `Authorization: Bearer`, принимаем ключ
        в имени или значении любого заголовка. Вынужденная мера: некоторые
        MCP-платформы в поле «заголовок» принимают только имя и подставляют
        значение сами, из-за чего ключ уезжает в имя заголовка
        (`Sk-Acbd…: acbd…`). Требовать идеальной формы от клиента дороже,
        чем принять ключ там, где он оказался.
        """
        if _matches(_header(scope, b"x-api-key"), self.api_key):
            return True
        if _matches(_header(scope, b"authorization"), self.api_key):
            return True

        for name, value in scope.get("headers") or []:
            if name.lower() in _IGNORED_HEADERS:
                continue
            if _matches(name.decode("latin-1"), self.api_key):
                return True
            if _matches(value.decode("latin-1"), self.api_key):
                return True
        return False

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"} or not self.api_key:
            await self.app(scope, receive, send)
            return

        path: str = scope.get("path", "")
        if any(path == p.rstrip("/") or path.startswith(p) for p in self.exempt):
            await self.app(scope, receive, send)
            return

        if not self._authorized(scope):
            response = JSONResponse(
                {
                    "error": "unauthorized",
                    "hint": (
                        "Передайте ключ одним из способов: X-API-Key: <key>, "
                        "Authorization: Bearer <key> или ключ в любом заголовке"
                    ),
                },
                status_code=401,
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)


class RateLimiter:
    """Простое окно на минуту: защищает дневную квоту API от случайного цикла."""

    def __init__(self, per_minute: int) -> None:
        self.per_minute = per_minute
        self._hits: dict[str, list[float]] = {}

    def allow(self, key: str) -> bool:
        if self.per_minute <= 0:
            return True
        now = time.monotonic()
        window = now - 60
        hits = [t for t in self._hits.get(key, []) if t > window]
        if len(hits) >= self.per_minute:
            self._hits[key] = hits
            return False
        hits.append(now)
        self._hits[key] = hits
        return True


class RateLimitMiddleware:
    def __init__(self, app: Any, *, limiter: RateLimiter) -> None:
        self.app = app
        self.limiter = limiter

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return

        path: str = scope.get("path", "")
        if path.startswith(("/healthz", "/readyz", "/files/")):
            await self.app(scope, receive, send)
            return

        if not self.limiter.allow(path):
            # Отвечаем 429, но с Retry-After: MCP-клиент без этой подсказки
            # считает сессию сломанной и падает с «client session is not running».
            response = JSONResponse(
                {
                    "error": "rate_limited",
                    "hint": (
                        f"Не больше {self.limiter.per_minute} запросов в минуту на эндпоинт. "
                        "Подождите и повторите; статус задачи можно не опрашивать — "
                        "результат скачивается фоновым поллером."
                    ),
                },
                status_code=429,
                headers={"Retry-After": "5"},
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)


# --- подпись ссылок на файлы ---------------------------------------------------


def sign_file(secret: str, rel_path: str, ttl_seconds: int) -> tuple[str, int]:
    """Возвращает (query-строка, unix-время истечения) для ссылки на файл."""
    expires = int(time.time()) + max(1, ttl_seconds)
    signature = hmac.new(
        secret.encode("utf-8"),
        f"{rel_path}|{expires}".encode(),
        hashlib.sha256,
    ).hexdigest()[:32]
    return f"exp={expires}&sig={signature}", expires


def verify_file(secret: str, rel_path: str, expires: str, signature: str) -> bool:
    try:
        exp = int(expires)
    except (TypeError, ValueError):
        return False
    if exp < int(time.time()):
        return False
    expected = hmac.new(
        secret.encode("utf-8"),
        f"{rel_path}|{exp}".encode(),
        hashlib.sha256,
    ).hexdigest()[:32]
    return hmac.compare_digest(expected, signature)
