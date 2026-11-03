"""Приёмочные тесты профиля GigaCowork.

Проверяют то, чего требует руководство «Разработка внешнего MCP для
GigaCowork»: версию протокола, уровни ошибок, ограничения и файловый режим.
Все тесты офлайн: настоящий Kandinsky API не вызывается, квота не тратится.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import shutil
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from starlette.testclient import TestClient

from kandinsky_mcp.api.client import DownloadedFile, TaskStatus
from kandinsky_mcp.api.errors import KandinskyError
from kandinsky_mcp.config import Settings
from kandinsky_mcp.domain.tasks import TaskManager
from kandinsky_mcp.server import build
from kandinsky_mcp.tools.session_files import download_read_url

API_KEY = "test-key"
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64
BIG_PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 300_000


def make_valid_png(size: int = 16) -> bytes:
    """Настоящий PNG: нужен там, где картинку разбирает PIL (вход edit/upscale)."""
    from io import BytesIO

    from PIL import Image

    buffer = BytesIO()
    Image.new("RGB", (size, size), (200, 30, 30)).save(buffer, format="PNG")
    return buffer.getvalue()

TOOL_NAMES = {
    "kandinsky_image_generate",
    "kandinsky_image_edit",
    "kandinsky_image_upscale",
    "kandinsky_task_status",
    "kandinsky_task_result",
    "kandinsky_tasks_list",
    "kandinsky_routes",
}
FILE_OUTPUT_TOOLS = {
    "kandinsky_image_generate",
    "kandinsky_image_edit",
    "kandinsky_image_upscale",
    "kandinsky_task_result",
}
READ_ONLY_TOOLS = {
    "kandinsky_task_status",
    "kandinsky_task_result",
    "kandinsky_tasks_list",
    "kandinsky_routes",
}
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


# --- окружение ---------------------------------------------------------------


class FakeClient:
    """Минимальный двойник Kandinsky API: одна успешная задача."""

    def __init__(self, payload: bytes = PNG, *, mime: str = "image/png") -> None:
        self.payload = payload
        self.mime = mime
        self.downloads = 0
        self.last_params: dict[str, Any] = {}
        self.calls = 0

    async def create_task(
        self, route: str, params: dict[str, Any], *, censor: bool | None = None
    ) -> str:
        self.calls += 1
        self.last_params = params
        return "101"

    async def get_status(self, task_id: str) -> TaskStatus:
        return TaskStatus(status="done")

    async def download_result(self, task_id: str) -> DownloadedFile:
        self.downloads += 1
        return DownloadedFile(content=self.payload, content_type=self.mime)

    async def health(self) -> bool:
        return True


def make_settings(data_dir: Path, **overrides: Any) -> Settings:
    data_dir.mkdir(parents=True, exist_ok=True)
    return Settings(
        kandinsky_token="token",
        mcp_api_key=API_KEY,
        file_secret="sign-key",
        data_dir=data_dir,
        public_base_url="https://kandinsky.example",
        route_sync=False,
        max_inline_bytes=1_000_000,
        **overrides,
    )


def make_workdir() -> Path:
    base = Path(__file__).parent / "_work" / f"gc-{uuid.uuid4().hex[:8]}"
    base.mkdir(parents=True, exist_ok=False)
    return base


@pytest.fixture
def bundle() -> Iterator[Any]:
    base = make_workdir()
    try:
        yield build(settings=make_settings(base), sync_routes=False)
    finally:
        shutil.rmtree(base, ignore_errors=True)


def install_fake(bundle: Any, payload: bytes = PNG) -> FakeClient:
    fake = FakeClient(payload)
    bundle.ctx.manager = TaskManager(
        fake,  # type: ignore[arg-type]
        bundle.ctx.store,
        bundle.ctx.registry,
        poll_min=0.01,
        poll_max=0.02,
        sleep=asyncio.sleep,
    )
    return fake


# --- HTTP-хелперы ------------------------------------------------------------


def sse_messages(text: str) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        data = "\n".join(
            line[5:].lstrip() for line in block.split("\n") if line.startswith("data:")
        ).strip()
        if not data:
            continue
        try:
            messages.append(json.loads(data))
        except json.JSONDecodeError:
            continue
    return messages


def by_id(text: str, request_id: str) -> dict[str, Any]:
    for message in sse_messages(text):
        if str(message.get("id")) == request_id:
            return message
    raise AssertionError(f"в ответе нет JSON-RPC сообщения с id={request_id}: {text[:300]}")


def rpc_headers(session_id: str | None = None, **extra: str) -> dict[str, str]:
    headers = {
        "Accept": "application/json, text/event-stream",
        "X-API-Key": API_KEY,
        "Content-Type": "application/json",
    }
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    headers.update(extra)
    return headers


def initialize(client: TestClient, **extra: str) -> tuple[str, dict[str, Any]]:
    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": "init-1",
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "gigacowork-compat", "version": "1"},
            },
        },
        headers=rpc_headers(**extra),
    )
    session_id = response.headers.get("mcp-session-id", "")
    if session_id:
        # Профиль требует подтвердить инициализацию до tools/list и tools/call.
        client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers=rpc_headers(session_id),
        )
    return session_id, by_id(response.text, "init-1")


def call(
    client: TestClient,
    session_id: str,
    payload: dict[str, Any],
    request_id: str = "call-1",
    **extra: str,
) -> dict[str, Any]:
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": payload},
        headers=rpc_headers(session_id, **extra),
    )
    return by_id(response.text, request_id)


@contextlib.contextmanager
def session(bundle: Any) -> Iterator[tuple[TestClient, str]]:
    with TestClient(bundle.app) as client:
        session_id, _ = initialize(client)
        assert session_id
        yield client, session_id


# --- протокол ----------------------------------------------------------------


def test_initialize_negotiates_2025_11_25(bundle: Any) -> None:
    with TestClient(bundle.app) as client:
        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": "init-1",
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "gigacowork-compat", "version": "1"},
                },
            },
            headers=rpc_headers(),
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers.get("mcp-session-id")
    result = by_id(response.text, "init-1")["result"]
    assert result["protocolVersion"] == "2025-11-25"
    assert result["serverInfo"]["name"]
    # Предварительная проверка адреса в GigaCowork — 256 KiB на ответ.
    assert len(response.text) < 256 * 1024


def test_initialized_notification_accepted(bundle: Any) -> None:
    with TestClient(bundle.app) as client:
        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": "init-1",
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "gigacowork-compat", "version": "1"},
                },
            },
            headers=rpc_headers(),
        )
        session_id = response.headers["mcp-session-id"]
        response = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers=rpc_headers(session_id),
        )
    assert response.status_code in (200, 202)
    assert response.text == ""


def test_tools_list_matches_profile(mcp_session: tuple[TestClient, str]) -> None:
    client, session_id = mcp_session
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": "list-1", "method": "tools/list"},
        headers=rpc_headers(session_id),
    )
    tools = by_id(response.text, "list-1")["result"]["tools"]
    names = [tool["name"] for tool in tools]

    assert set(names) == TOOL_NAMES
    assert len(names) == len(set(names))
    assert all(NAME_RE.match(name) for name in names)
    # GigaCowork ждёт весь обязательный каталог в первом ответе.
    assert "nextCursor" not in by_id(response.text, "list-1")["result"]

    for tool in tools:
        assert tool.get("description")
        schema = tool["inputSchema"]
        assert schema["type"] == "object"
        assert isinstance(schema.get("properties"), dict)
        assert tool.get("outputSchema"), tool["name"]
        annotations = tool.get("annotations") or {}
        if tool["name"] in FILE_OUTPUT_TOOLS:
            assert annotations.get("x-session-file-output") == "url", tool["name"]
        if tool["name"] in READ_ONLY_TOOLS:
            assert annotations.get("readOnlyHint") is True, tool["name"]


async def test_large_wait_seconds_is_clamped_not_rejected(bundle: Any) -> None:
    """GigaCowork-модель может попросить wait_seconds=90 — это не ошибка.

    Раньше схема с le=60 отбивала такой вызов JSON-RPC -32602, а платформа
    показывала «Internal error». Теперь аргумент принимается и ограничивается
    бюджетом вызова.
    """
    install_fake(bundle)
    tools = {tool.name: tool for tool in await bundle.mcp.list_tools()}
    wait_schema = tools["kandinsky_image_generate"].input_schema["properties"]["wait_seconds"]
    assert "maximum" not in wait_schema

    result = await bundle.mcp.call_tool(
        "kandinsky_image_generate",
        {"prompt": "кот", "wait_seconds": 1000},
    )
    assert result.is_error is False


async def test_session_file_input_marker(mcp_session: tuple[TestClient, str]) -> None:
    client, session_id = mcp_session
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": "list-1", "method": "tools/list"},
        headers=rpc_headers(session_id),
    )
    tools = {tool["name"]: tool for tool in by_id(response.text, "list-1")["result"]["tools"]}

    for name in ("kandinsky_image_edit", "kandinsky_image_upscale"):
        schema = tools[name]["inputSchema"]
        read_url = schema["properties"]["read_url"]
        marker = read_url["x-session-file-input"]
        # Ровно два ключа, транспорт read_url, model_field не совпадает с полем.
        assert set(marker) == {"model_field", "transport"}
        assert marker["transport"] == "read_url"
        assert marker["model_field"] == "source"
        assert marker["model_field"] not in schema["properties"]
        required = schema.get("required", [])
        assert "read_url" not in required
        assert "images" not in required
        assert "image" not in required


def test_unknown_tool_is_jsonrpc_invalid_params(mcp_session: tuple[TestClient, str]) -> None:
    client, session_id = mcp_session
    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": "call-1",
            "method": "tools/call",
            "params": {"name": "records_search", "arguments": {}},
        },
        headers=rpc_headers(session_id),
    )
    message = by_id(response.text, "call-1")
    assert "result" not in message
    assert message["error"]["code"] == -32602


def test_invalid_arguments_is_jsonrpc_invalid_params(
    mcp_session: tuple[TestClient, str],
) -> None:
    client, session_id = mcp_session
    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": "call-1",
            "method": "tools/call",
            "params": {"name": "kandinsky_task_status", "arguments": {}},
        },
        headers=rpc_headers(session_id),
    )
    message = by_id(response.text, "call-1")
    assert message["error"]["code"] == -32602
    assert "task_id" in message["error"]["message"]
    assert "pydantic" not in message["error"]["message"]


def test_application_error_is_iserror(bundle: Any) -> None:
    install_fake(bundle)
    with session(bundle) as (client, session_id):
        message = call(client, session_id, {"name": "kandinsky_task_result", "arguments": {"task_id": "nope"}})
    result = message["result"]
    assert result["isError"] is True
    assert "not_found" in result["content"][0]["text"]


def test_get_mcp_without_session_is_405(bundle: Any) -> None:
    with TestClient(bundle.app) as client:
        response = client.get("/mcp", headers={"X-API-Key": API_KEY})
    assert response.status_code == 405
    assert "POST" in response.headers.get("allow", "")


def test_origin_policy(bundle: Any, allowed_origin_bundle: Any) -> None:
    with TestClient(bundle.app) as client:
        evil = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": "init-1", "method": "initialize", "params": {}},
            headers=rpc_headers(Origin="https://evil.example"),
        )
        same_origin = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": "init-1",
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "gigacowork", "version": "1"},
                },
            },
            headers=rpc_headers(Origin="https://kandinsky.example"),
        )
    assert evil.status_code == 403
    # PUBLIC_BASE_URL доверяется как same-origin.
    assert same_origin.status_code == 200

    with TestClient(allowed_origin_bundle.app) as client:
        allowed = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": "init-1",
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "gigacowork", "version": "1"},
                },
            },
            headers=rpc_headers(Origin="https://cowork.ru"),
        )
        evil = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": "init-1", "method": "initialize", "params": {}},
            headers=rpc_headers(Origin="https://evil.example"),
        )
    assert allowed.status_code == 200
    assert evil.status_code == 403


def test_mcp_requires_api_key(bundle: Any) -> None:
    with TestClient(bundle.app) as client:
        response = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"Accept": "application/json, text/event-stream"},
        )
    assert response.status_code == 401


# --- результат инструмента ---------------------------------------------------


async def test_saved_result_has_file_mode_and_link_in_text(bundle: Any) -> None:
    fake = install_fake(bundle)
    result = await bundle.mcp.call_tool(
        "kandinsky_image_generate",
        {"prompt": "закат над морем", "wait_seconds": 5},
    )

    assert result.is_error is False
    data = result.structured_content
    assert data["status"] == "saved"
    assert data["filename"] == "101.png"
    assert data["content_type"] == "image/png"
    assert data["download_url"].startswith("https://kandinsky.example/files/")
    assert data["size"] == len(PNG)
    assert fake.calls == 1

    text = "\n".join(block.text for block in result.content if block.type == "text")
    # GigaCowork не переносит файл в сессию сам, поэтому ссылка нужна в тексте.
    assert "download_url: https://kandinsky.example/files/" in text
    assert "101.png" in text
    # Маленькая картинка приходит превью (по умолчанию бюджет отключён).
    assert any(block.type == "image" for block in result.content)


async def test_large_image_still_has_preview_by_default(bundle: Any) -> None:
    install_fake(bundle, BIG_PNG)
    result = await bundle.mcp.call_tool(
        "kandinsky_image_generate",
        {"prompt": "большая картинка", "wait_seconds": 5},
    )

    assert result.is_error is False
    text = "\n".join(block.text for block in result.content if block.type == "text")
    image_len = sum(len(block.data) for block in result.content if block.type == "image")
    # Текст компактный, превью не режется: иначе GigaCowork не покажет картинку.
    assert len(text) < 100_000
    assert image_len > 0
    assert result.structured_content["download_url"].startswith("https://")


async def test_inline_budget_can_disable_preview() -> None:
    base = make_workdir()
    try:
        limited = build(
            settings=make_settings(base, max_inline_result_chars=10_000), sync_routes=False
        )
        install_fake(limited, BIG_PNG)
        result = await limited.mcp.call_tool(
            "kandinsky_image_generate",
            {"prompt": "большая картинка", "wait_seconds": 5},
        )
    finally:
        shutil.rmtree(base, ignore_errors=True)

    image_len = sum(len(block.data) for block in result.content if block.type == "image")
    assert image_len == 0
    assert result.structured_content["download_url"].startswith("https://")


async def test_read_url_is_downloaded_and_never_exposed(bundle: Any, monkeypatch: Any) -> None:
    fake = install_fake(bundle)

    async def fake_download(url: str, *, max_bytes: int, timeout: float) -> bytes:
        assert url == "https://files.example/source.png"
        return make_valid_png()

    monkeypatch.setattr("kandinsky_mcp.tools.image.download_read_url", fake_download)

    result = await bundle.mcp.call_tool(
        "kandinsky_image_edit",
        {"prompt": "сделай акварелью", "read_url": "https://files.example/source.png", "wait_seconds": 5},
    )

    assert result.is_error is False
    assert fake.last_params["image"], "скачанный файл должен уйти в API"
    text = "\n".join(block.text for block in result.content if block.type == "text")
    rendered = json.dumps(result.structured_content, ensure_ascii=False)
    # Временная read_url не должна попасть ни в текст, ни в structuredContent.
    assert "files.example" not in rendered
    assert "files.example" not in text
    assert "source.png" not in text


async def test_read_url_rejects_http_url() -> None:
    with pytest.raises(KandinskyError) as excinfo:
        await download_read_url("http://files.example/a.png", max_bytes=1000, timeout=5)
    assert "files.example" not in excinfo.value.message
    assert "files.example" not in excinfo.value.hint


@respx.mock
async def test_read_url_size_limit() -> None:
    respx.get("https://files.example/big.png").mock(
        return_value=httpx.Response(200, content=b"x" * 64)
    )
    with pytest.raises(KandinskyError):
        await download_read_url("https://files.example/big.png", max_bytes=10, timeout=5)


def test_download_url_is_served_without_compression(bundle: Any) -> None:
    from kandinsky_mcp.auth import sign_file
    from kandinsky_mcp.domain.store import TaskRecord

    record = TaskRecord(task_id="55", route="k6-image-t2i", kind="image", prompt="кот")
    stored = bundle.ctx.store.store_result(record, PNG, "image/png", ".png")
    query, _ = sign_file(bundle.ctx.settings.signing_key, stored.rel_path, 600)

    with TestClient(bundle.app) as client:
        response = client.get(f"/files/{stored.rel_path}?{query}")

    assert response.status_code == 200
    assert response.headers.get("content-encoding") == "identity"
    assert response.headers["content-type"].startswith("image/png")
    assert response.content == PNG


# --- фикстуры для протокольных тестов ---------------------------------------


@pytest.fixture
def allowed_origin_bundle() -> Iterator[Any]:
    base = make_workdir()
    try:
        yield build(
            settings=make_settings(base, mcp_allowed_origins="https://cowork.ru"),
            sync_routes=False,
        )
    finally:
        shutil.rmtree(base, ignore_errors=True)


@pytest.fixture
def mcp_session(bundle: Any) -> Iterator[tuple[TestClient, str]]:
    with TestClient(bundle.app) as client:
        session_id, _ = initialize(client)
        assert session_id
        yield client, session_id
