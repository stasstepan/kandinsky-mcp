#!/usr/bin/env python3
"""Проверка совместимости Kandinsky MCP с профилем GigaCowork (§3.9 руководства).

Скрипт повторяет воспроизводимый сценарий проверки внешнего MCP из руководства
«Разработка внешнего MCP для GigaCowork»:

    initialize → notifications/initialized → tools/list → tools/call
    → негативные кейсы → DELETE

Каждый пункт печатается как PASS или FAIL с причиной, в конце — сводка.
Нужен только Python 3.10+ и стандартная библиотека (urllib, ssl, json, re, os),
сторонних зависимостей нет.

Запуск:

    PowerShell:
        $env:MCP_API_KEY='<ключ>'
        python scripts/compat_check.py

    bash:
        MCP_API_KEY='<ключ>' python scripts/compat_check.py

Переменные окружения:

    MCP_URL        адрес MCP (по умолчанию https://kandinsky.stasstep.ru/mcp)
    MCP_API_KEY    ключ доступа X-API-Key (обязателен)

Квота Kandinsky API не расходуется: из инструментов вызывается только
бесплатная справка kandinsky_routes, остальные вызовы заведомо отклоняются
сервером до обращения к API (неизвестный инструмент, отсутствующий аргумент,
неверный ключ, чужой Origin).

Код возврата:
    0 — все проверки прошли;
    1 — есть FAIL (сервер не соответствует профилю);
    2 — ошибка конфигурации или сети (нет ключа, адрес недоступен и т.п.).
"""

from __future__ import annotations

import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.request

MCP_URL_DEFAULT = "https://kandinsky.stasstep.ru/mcp"
PROTOCOL_VERSION = "2025-11-25"
REQUEST_TIMEOUT = 30  # секунды на один HTTP-запрос
BODY_PREVIEW = 200  # сколько символов тела ответа показывать в причине FAIL

TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

REQUIRED_TOOLS = (
    "kandinsky_image_generate",
    "kandinsky_image_edit",
    "kandinsky_image_upscale",
    "kandinsky_task_status",
    "kandinsky_task_result",
    "kandinsky_tasks_list",
    "kandinsky_routes",
)

SSL_CONTEXT = ssl.create_default_context()


class ConfigError(RuntimeError):
    """Неверная конфигурация проверки (например, ключ отклонён сервером)."""


class NetworkError(RuntimeError):
    """Сеть или TLS недоступны — продолжать проверки нельзя."""


class HttpResult:
    """Минимальное представление HTTP-ответа: статус, заголовки, тело."""

    __slots__ = ("status", "headers", "body", "content_type")

    def __init__(self, status: int, headers: dict[str, str], body: bytes) -> None:
        self.status = status
        self.headers = headers
        self.body = body
        self.content_type = headers.get("content-type", "").split(";")[0].strip().lower()


class Report:
    """Счётчик PASS/FAIL и вывод сводки."""

    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0

    def check(self, ok: bool, name: str, detail: str = "") -> bool:
        if ok:
            self.passed += 1
            print(f"PASS  {name}")
        else:
            self.failed += 1
            line = f"FAIL  {name}"
            if detail:
                line += f" — {detail}"
            print(line)
        return ok

    def summary(self) -> int:
        total = self.passed + self.failed
        print()
        print(f"Итог: {self.passed} PASS, {self.failed} FAIL (всего {total})")
        if self.failed:
            print("Сервер пока не соответствует профилю GigaCowork — см. FAIL выше.")
            return 1
        print("Все проверки прошли: сервер соответствует сценарию §3.9.")
        return 0


def section(title: str) -> None:
    print()
    print(f"[{title}]")


def _headers_dict(headers) -> dict[str, str]:
    if headers is None:
        return {}
    return {key.lower(): value for key, value in headers.items()}


def http_request(
    method: str,
    url: str,
    *,
    api_key: str,
    headers: dict[str, str] | None = None,
    payload: dict | None = None,
) -> HttpResult:
    """Выполняет один HTTP-запрос и возвращает ответ, не бросая исключений на 4xx/5xx."""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request_headers = {
        "Accept": "application/json, text/event-stream",
        "X-API-Key": api_key,
        "User-Agent": "kandinsky-mcp-compat-check/1",
    }
    if body is not None:
        request_headers["Content-Type"] = "application/json"
    if headers:
        request_headers.update(headers)

    request = urllib.request.Request(url, data=body, headers=request_headers, method=method)
    try:
        response = urllib.request.urlopen(
            request,
            timeout=REQUEST_TIMEOUT,
            context=SSL_CONTEXT,
        )
    except urllib.error.HTTPError as exc:
        return HttpResult(exc.code, _headers_dict(exc.headers), exc.read())
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        raise NetworkError(f"не удалось подключиться к {url}: {reason}") from exc
    except TimeoutError as exc:
        raise NetworkError(f"таймаут {REQUEST_TIMEOUT} с при запросе к {url}") from exc
    except OSError as exc:
        raise NetworkError(f"сетевая ошибка при запросе к {url}: {exc}") from exc

    with response:
        return HttpResult(response.status, _headers_dict(response.headers), response.read())


def post_rpc(
    url: str,
    payload: dict,
    api_key: str,
    extra_headers: dict[str, str] | None = None,
) -> HttpResult:
    return http_request("POST", url, api_key=api_key, headers=extra_headers, payload=payload)


def session_headers(session_id: str) -> dict[str, str]:
    headers = {"MCP-Protocol-Version": PROTOCOL_VERSION}
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    return headers


def sse_data_payloads(text: str) -> list[str]:
    """Достаёт значения полей data из SSE-потока (по границам событий)."""
    payloads: list[str] = []
    data_lines: list[str] = []
    for line in text.splitlines():
        if not line:
            if data_lines:
                payloads.append("\n".join(data_lines))
                data_lines = []
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "data":
            data_lines.append(value)
    if data_lines:
        payloads.append("\n".join(data_lines))
    return payloads


def jsonrpc_message(http: HttpResult, expected_id: str) -> tuple[dict | None, str]:
    """Ищет JSON-RPC-сообщение с нужным id в JSON- или SSE-ответе."""
    body_text = http.body.decode("utf-8", "replace")
    if http.content_type == "text/event-stream":
        candidates = sse_data_payloads(body_text)
    elif http.content_type in ("application/json", ""):
        candidates = [body_text]
    else:
        return None, f"неожиданный Content-Type {http.content_type!r} (HTTP {http.status})"

    for raw in candidates:
        try:
            message = json.loads(raw)
        except ValueError:
            continue
        if isinstance(message, dict) and message.get("id") == expected_id:
            return message, ""

    reason = (
        f"в ответе нет JSON-RPC-сообщения с id {expected_id!r} "
        f"(HTTP {http.status}, Content-Type: {http.content_type or 'не указан'})"
    )
    return None, reason


def preview(http: HttpResult) -> str:
    text = http.body.decode("utf-8", "replace").strip()
    if not text:
        return "пустое тело"
    return text[:BODY_PREVIEW] + ("…" if len(text) > BODY_PREVIEW else "")


def rpc_error(message: dict | None) -> dict | None:
    if isinstance(message, dict) and isinstance(message.get("error"), dict):
        return message["error"]
    return None


def is_invalid_params(message: dict | None) -> bool:
    """JSON-RPC error -32602 и нет поля result — ожидаемый ответ на плохие параметры."""
    error = rpc_error(message)
    return error is not None and error.get("code") == -32602 and "result" not in message


def describe_negative(message: dict | None, fallback: str) -> str:
    if message is None:
        return fallback
    error = rpc_error(message)
    if error is not None:
        return f"JSON-RPC error с кодом {error.get('code')!r}: {str(error.get('message'))[:120]}"
    result = message.get("result")
    if isinstance(result, dict):
        if result.get("isError"):
            return "result с isError=true — прикладная ошибка вместо JSON-RPC error -32602"
        return "result вместо JSON-RPC error -32602"
    return "ответ без полей error/result"


def run_checks(url: str, api_key: str) -> Report:
    report = Report()

    section("Жизненный цикл MCP")
    init_payload = {
        "jsonrpc": "2.0",
        "id": "init-1",
        "method": "initialize",
        "params": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "gigacowork-compat-check", "version": "1"},
        },
    }
    init_http = post_rpc(url, init_payload, api_key)
    if init_http.status == 401:
        raise ConfigError("MCP_API_KEY отклонён сервером: HTTP 401. Проверьте ключ доступа.")

    report.check(
        200 <= init_http.status < 300,
        "initialize: HTTP 2xx",
        f"получен HTTP {init_http.status}: {preview(init_http)}",
    )
    report.check(
        init_http.content_type in ("application/json", "text/event-stream"),
        "initialize: Content-Type application/json или text/event-stream",
        f"получен {init_http.content_type or 'без Content-Type'}",
    )
    init_message, init_reason = jsonrpc_message(init_http, "init-1")
    report.check(init_message is not None, 'initialize: ответ с id "init-1"', init_reason)
    init_result = init_message.get("result") if init_message else None
    protocol = init_result.get("protocolVersion") if isinstance(init_result, dict) else None
    report.check(
        protocol == PROTOCOL_VERSION,
        f'initialize: result.protocolVersion == "{PROTOCOL_VERSION}"',
        f"получено {protocol!r}",
    )
    session_id = init_http.headers.get("mcp-session-id", "").strip()
    report.check(
        bool(session_id),
        "initialize: заголовок MCP-Session-Id",
        "заголовок отсутствует — сервер не создал сессию",
    )

    initialized_http = post_rpc(
        url,
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        api_key,
        session_headers(session_id),
    )
    report.check(
        200 <= initialized_http.status < 300,
        "notifications/initialized: HTTP 2xx (ожидается 202)",
        f"получен HTTP {initialized_http.status}: {preview(initialized_http)}",
    )

    section("tools/list")
    list_http = post_rpc(
        url,
        {"jsonrpc": "2.0", "id": "list-1", "method": "tools/list"},
        api_key,
        session_headers(session_id),
    )
    list_message, list_reason = jsonrpc_message(list_http, "list-1")
    report.check(list_message is not None, 'tools/list: ответ с id "list-1"', list_reason)
    list_result = list_message.get("result") if list_message else None
    tools = list_result.get("tools") if isinstance(list_result, dict) else None
    tools_is_list = isinstance(tools, list)
    report.check(
        tools_is_list,
        "tools/list: result.tools — массив",
        "result.tools отсутствует" if not tools_is_list else "",
    )

    tool_names: list[str] = []
    if tools_is_list:
        tool_names = [
            tool.get("name")
            for tool in tools
            if isinstance(tool, dict) and isinstance(tool.get("name"), str)
        ]
    missing = [name for name in REQUIRED_TOOLS if name not in set(tool_names)]
    report.check(
        tools_is_list and not missing,
        "tools/list: все 7 обязательных инструментов в первом ответе",
        f"нет: {', '.join(missing)}" if missing else "result.tools отсутствует",
    )
    invalid_names = [name for name in tool_names if not TOOL_NAME_RE.fullmatch(name)]
    report.check(
        not invalid_names,
        "tools/list: имена соответствуют ^[A-Za-z0-9_-]{1,64}$",
        f"невалидные имена: {', '.join(invalid_names)}" if invalid_names else "",
    )
    duplicates = sorted({name for name in tool_names if tool_names.count(name) > 1})
    report.check(
        not duplicates,
        "tools/list: нет дублей имён",
        f"дубли: {', '.join(duplicates)}" if duplicates else "",
    )
    next_cursor = list_result.get("nextCursor") if isinstance(list_result, dict) else None
    report.check(
        not next_cursor,
        "tools/list: без пагинации в первом ответе",
        f"в ответе nextCursor={next_cursor!r} — обязательные инструменты должны быть сразу",
    )

    section("tools/call (бесплатный kandinsky_routes, квота не тратится)")
    call_http = post_rpc(
        url,
        {
            "jsonrpc": "2.0",
            "id": "call-1",
            "method": "tools/call",
            "params": {"name": "kandinsky_routes", "arguments": {}},
        },
        api_key,
        session_headers(session_id),
    )
    call_message, call_reason = jsonrpc_message(call_http, "call-1")
    report.check(call_message is not None, 'tools/call: ответ с id "call-1"', call_reason)
    call_result = call_message.get("result") if call_message else None
    result_is_dict = isinstance(call_result, dict)
    report.check(
        result_is_dict,
        "tools/call: result присутствует",
        "получен JSON-RPC error вместо result" if not result_is_dict else "",
    )
    report.check(
        result_is_dict and not call_result.get("isError"),
        "tools/call: isError отсутствует или false",
        "сервер вернул прикладную ошибку (isError=true)" if result_is_dict else "",
    )
    structured = call_result.get("structuredContent") if result_is_dict else None
    report.check(
        isinstance(structured, dict),
        "tools/call: structuredContent присутствует",
        "structuredContent отсутствует — GigaCowork не сможет проверить результат",
    )

    section("Негативные кейсы")
    unknown_http = post_rpc(
        url,
        {
            "jsonrpc": "2.0",
            "id": "call-unknown",
            "method": "tools/call",
            "params": {"name": "kandinsky_no_such_tool", "arguments": {}},
        },
        api_key,
        session_headers(session_id),
    )
    unknown_message, unknown_reason = jsonrpc_message(unknown_http, "call-unknown")
    report.check(
        is_invalid_params(unknown_message),
        "неизвестный инструмент: JSON-RPC error -32602",
        describe_negative(unknown_message, unknown_reason),
    )

    missing_arg_http = post_rpc(
        url,
        {
            "jsonrpc": "2.0",
            "id": "call-missing-arg",
            "method": "tools/call",
            "params": {"name": "kandinsky_task_status", "arguments": {}},
        },
        api_key,
        session_headers(session_id),
    )
    missing_arg_message, missing_arg_reason = jsonrpc_message(missing_arg_http, "call-missing-arg")
    report.check(
        is_invalid_params(missing_arg_message),
        "kandinsky_task_status без task_id: JSON-RPC error -32602",
        describe_negative(missing_arg_message, missing_arg_reason),
    )

    try:
        get_http = http_request(
            "GET",
            url,
            api_key=api_key,
            headers={"Accept": "application/json, text/event-stream"},
        )
    except NetworkError as exc:
        report.check(False, "GET /mcp без сессии: 405 Method Not Allowed", str(exc))
    else:
        report.check(
            get_http.status == 405,
            "GET /mcp без сессии: 405 Method Not Allowed",
            f"получен HTTP {get_http.status}: {preview(get_http)}",
        )

    origin_http = post_rpc(
        url,
        init_payload,
        api_key,
        {"Origin": "https://evil.example"},
    )
    report.check(
        origin_http.status == 403,
        "POST с Origin: https://evil.example: 403 Forbidden",
        f"получен HTTP {origin_http.status}: {preview(origin_http)}",
    )

    wrong_key_http = post_rpc(
        url,
        init_payload,
        "definitely-invalid-key-for-compat-check",
    )
    report.check(
        wrong_key_http.status == 401,
        "неверный X-API-Key: 401 Unauthorized",
        f"получен HTTP {wrong_key_http.status}: {preview(wrong_key_http)}",
    )

    section("Завершение")
    if session_id:
        delete_http = http_request(
            "DELETE", url, api_key=api_key, headers=session_headers(session_id)
        )
        report.check(
            delete_http.status in (200, 202, 204),
            "DELETE /mcp: 200/202/204",
            f"получен HTTP {delete_http.status}: {preview(delete_http)}",
        )
    else:
        report.check(
            False,
            "DELETE /mcp: 200/202/204",
            "нет Mcp-Session-Id — сессию не создать и не закрыть",
        )

    return report


def main() -> int:
    url = (os.environ.get("MCP_URL") or "").strip() or MCP_URL_DEFAULT
    api_key = (os.environ.get("MCP_API_KEY") or "").strip()

    print("=== Проверка совместимости Kandinsky MCP с GigaCowork (сценарий §3.9) ===")
    print(f"Адрес MCP: {url}")

    if not api_key:
        print(
            "ОШИБКА: не задан MCP_API_KEY — ключ доступа X-API-Key для MCP-сервера.",
            file=sys.stderr,
        )
        print("Задайте его в окружении и повторите запуск:", file=sys.stderr)
        print(
            "  PowerShell:  $env:MCP_API_KEY='<ключ>'; python scripts/compat_check.py",
            file=sys.stderr,
        )
        print(
            "  bash:        MCP_API_KEY='<ключ>' python scripts/compat_check.py",
            file=sys.stderr,
        )
        return 2

    print("X-API-Key: задан в MCP_API_KEY (значение не печатается)")

    try:
        report = run_checks(url, api_key)
    except (ConfigError, NetworkError) as exc:
        print()
        print(f"ОШИБКА: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print()
        print("Проверка прервана пользователем.", file=sys.stderr)
        return 2

    return report.summary()


if __name__ == "__main__":
    sys.exit(main())
