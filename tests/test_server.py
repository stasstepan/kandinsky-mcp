"""Сборка сервера: состав инструментов, доступ по ключу, доставка файлов."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from kandinsky_mcp.api.client import DownloadedFile, TaskStatus
from kandinsky_mcp.api.errors import KandinskyError
from kandinsky_mcp.auth import sign_file
from kandinsky_mcp.config import Settings
from kandinsky_mcp.domain.tasks import TaskManager
from kandinsky_mcp.server import build

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 40


class FakeClient:
    def __init__(self, statuses: list[str] | None = None, *, fail_with: str | None = None) -> None:
        self._statuses = statuses or ["done"]
        self._fail_with = fail_with
        self.downloads = 0

    async def create_task(self, route: str, params: dict[str, Any], *, censor: bool | None = None) -> str:
        if self._fail_with:
            raise KandinskyError("censored", self._fail_with, 422)
        return "101"

    async def get_status(self, task_id: str) -> TaskStatus:
        status = self._statuses.pop(0) if len(self._statuses) > 1 else self._statuses[0]
        return TaskStatus(status=status)

    async def download_result(self, task_id: str) -> DownloadedFile:
        self.downloads += 1
        return DownloadedFile(content=PNG, content_type="image/png")

    async def health(self) -> bool:
        return True


def make_settings(data_dir: Path) -> Settings:
    return Settings(
        kandinsky_token="test-token",
        mcp_api_key="secret-key",
        file_secret="sign-key",
        mcp_transport="http",
        data_dir=data_dir,
        public_base_url="https://kandinsky.example",
        route_sync=False,
        max_inline_bytes=1_000_000,
    )


@pytest.fixture
def bundle(workdir: Path):
    from starlette.testclient import TestClient  # noqa: F401  (импорт для совместимости)

    return build(settings=make_settings(workdir), sync_routes=False)


def test_all_expected_tools_are_registered(bundle) -> None:
    tools = asyncio.run(bundle.mcp.list_tools())
    names = sorted(t.name for t in tools)
    assert names == [
        "kandinsky_image_edit",
        "kandinsky_image_generate",
        "kandinsky_image_upscale",
        "kandinsky_routes",
        "kandinsky_task_result",
        "kandinsky_task_status",
        "kandinsky_tasks_list",
    ]


def test_tool_schemas_expose_enums(bundle) -> None:
    """Строгие enum в схеме — главный способ не дать модели выдумать значение."""
    tools = {t.name: t for t in asyncio.run(bundle.mcp.list_tools())}
    generate = tools["kandinsky_image_generate"]
    props = generate.input_schema["properties"]
    assert props["model"]["enum"] == ["k6", "k5"]
    assert "1024x1024" in str(props["resolution"])
    assert "prompt" in generate.input_schema["required"]


def test_routes_tool_lists_only_exposed(bundle) -> None:
    result = asyncio.run(bundle.mcp.call_tool("kandinsky_routes", {}))
    data = result.structured_content
    assert data["count"] == 5
    assert data["api_host"].startswith("https://")
    assert all(r["tool"] for r in data["routes"])


def test_generate_tool_returns_saved_file_with_preview(bundle) -> None:
    """Полный путь: задача → фоновый поллер → файл → inline-превью."""
    fake = FakeClient()
    bundle.ctx.manager = TaskManager(
        fake,  # type: ignore[arg-type]
        bundle.ctx.store,
        bundle.ctx.registry,
        poll_min=0.01,
        poll_max=0.02,
        sleep=asyncio.sleep,
    )

    result = asyncio.run(
        bundle.mcp.call_tool(
            "kandinsky_image_generate",
            {"prompt": "закат над морем", "resolution": "1024:1024", "wait_seconds": 5},
        )
    )

    assert result.is_error is False
    data = result.structured_content
    assert data["status"] == "saved"
    assert data["params"]["resolution"] == "1024x1024"  # двоеточие нормализовано
    assert data["media"][0]["url"].startswith("https://kandinsky.example/files/")
    assert data["media"][0]["bytes"] == len(PNG)
    kinds = [c.type for c in result.content]
    assert kinds == ["text", "image"]


def test_generate_tool_surfaces_censor_as_actionable_error(bundle) -> None:
    bundle.ctx.manager = TaskManager(
        FakeClient(fail_with="text blocked by censor: unsafe content"),  # type: ignore[arg-type]
        bundle.ctx.store,
        bundle.ctx.registry,
        sleep=asyncio.sleep,
    )

    result = asyncio.run(
        bundle.mcp.call_tool("kandinsky_image_generate", {"prompt": "что-то", "wait_seconds": 0})
    )

    assert result.is_error is True
    assert "censored" in result.content[0].text
    assert "перепишите промпт" in result.content[0].text.lower()


def test_task_result_is_safe_to_repeat(bundle) -> None:
    """Повторный вызов результата не ходит в API — иначе файл был бы потерян."""
    fake = FakeClient()
    bundle.ctx.manager = TaskManager(
        fake,  # type: ignore[arg-type]
        bundle.ctx.store,
        bundle.ctx.registry,
        poll_min=0.01,
        poll_max=0.02,
        sleep=asyncio.sleep,
    )
    created = asyncio.run(
        bundle.mcp.call_tool("kandinsky_image_generate", {"prompt": "кот", "wait_seconds": 5})
    )
    task_id = created.structured_content["task_id"]

    first = asyncio.run(bundle.mcp.call_tool("kandinsky_task_result", {"task_id": task_id}))
    second = asyncio.run(bundle.mcp.call_tool("kandinsky_task_result", {"task_id": task_id}))

    assert first.is_error is False
    assert second.is_error is False
    assert fake.downloads == 1


def test_unknown_task_is_reported_not_crashed(bundle) -> None:
    result = asyncio.run(bundle.mcp.call_tool("kandinsky_task_status", {"task_id": "nope"}))
    assert result.is_error is True
    assert "not_found" in result.content[0].text


# --- HTTP-слой ---------------------------------------------------------------


def asgi_client(bundle) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=bundle.app),
        base_url="http://testserver",
    )


async def test_mcp_endpoint_requires_api_key(bundle) -> None:
    async with asgi_client(bundle) as client:
        response = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"Accept": "application/json, text/event-stream"},
        )
    assert response.status_code == 401


async def test_healthz_is_public(bundle) -> None:
    async with asgi_client(bundle) as client:
        response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


async def test_readyz_reports_api_state(bundle, monkeypatch) -> None:
    """Готовность проверяется без обращения к настоящему API.

    Раньше здесь был реальный сетевой вызов: локально он проходил, а на
    раннере GitHub внешний запрос не удавался, и тест падал с 503.
    """

    async def healthy() -> bool:
        return True

    monkeypatch.setattr(bundle.ctx.client, "health", healthy)

    async with asgi_client(bundle) as client:
        response = await client.get("/readyz")

    assert response.status_code == 200
    body = response.json()
    assert body["kandinsky_api"] == "reachable"
    assert body["status"] == "ok"


async def test_readyz_reports_degraded_when_api_is_down(bundle, monkeypatch) -> None:
    """Обратная ветка: API недоступен — 503 и понятный статус."""

    async def unreachable() -> bool:
        return False

    monkeypatch.setattr(bundle.ctx.client, "health", unreachable)

    async with asgi_client(bundle) as client:
        response = await client.get("/readyz")

    assert response.status_code == 503
    body = response.json()
    assert body["kandinsky_api"] == "unreachable"
    assert body["status"] == "degraded"


async def test_files_require_valid_signature(bundle) -> None:
    record = bundle.ctx.store.load_task("x")
    assert record is None

    # положим файл напрямую
    from kandinsky_mcp.domain.store import TaskRecord

    task = TaskRecord(task_id="55", route="k6-image-t2i", kind="image", prompt="кот")
    stored = bundle.ctx.store.store_result(task, PNG, "image/png", ".png")
    query, _ = sign_file(bundle.ctx.settings.signing_key, stored.rel_path, 600)

    async with asgi_client(bundle) as client:
        ok = await client.get(f"/files/{stored.rel_path}?{query}")
        bad = await client.get(f"/files/{stored.rel_path}?exp=99999999999&sig=deadbeef")
        unsigned = await client.get(f"/files/{stored.rel_path}")

    assert ok.status_code == 200
    assert ok.headers["content-type"].startswith("image/png")
    assert ok.content == PNG
    assert bad.status_code == 403
    assert unsigned.status_code == 403


async def test_expired_signature_is_rejected(bundle) -> None:
    from kandinsky_mcp.auth import sign_file as sign

    past = str(int(time.time()) - 10)
    import hashlib
    import hmac

    secret = bundle.ctx.settings.signing_key
    rel = "results/2026-01-01/1.png"
    sig = hmac.new(secret.encode(), f"{rel}|{past}".encode(), hashlib.sha256).hexdigest()[:32]

    async with asgi_client(bundle) as client:
        response = await client.get(f"/files/{rel}?exp={past}&sig={sig}")
    assert response.status_code == 403
    assert sign is not None


# --- return_base64 (по спецификации заказчика) -------------------------------


def make_saved_task(bundle, task_id="900", payload=None, mime="image/png", ext=".png"):
    """Кладёт в хранилище уже готовую задачу с файлом на диске."""
    from kandinsky_mcp.domain.store import TaskRecord

    data = payload if payload is not None else PNG
    record = TaskRecord(
        task_id=task_id,
        route="k6-image-t2i",
        kind="image",
        prompt="кот",
        status="saved",
    )
    stored = bundle.ctx.store.store_result(record, data, mime, ext)
    record.files = [stored]
    record.finished_at = record.created_at + 1
    bundle.ctx.store.save_task(record)
    return record


def test_task_result_without_base64_is_backward_compatible(bundle) -> None:
    """Критерий 1: параметр не передан — ответ как раньше, без b64."""
    make_saved_task(bundle, task_id="901")

    result = asyncio.run(bundle.mcp.call_tool("kandinsky_task_result", {"task_id": "901"}))

    media = result.structured_content["media"][0]
    assert "b64" not in media
    assert "b64_error" not in media
    assert media["bytes"] == len(PNG)
    assert media["url"].startswith("https://")


def test_task_result_returns_decodable_base64(bundle) -> None:
    """Критерий 2: b64 декодируется, длина и sha256 совпадают с полями ответа."""
    import base64
    import hashlib

    record = make_saved_task(bundle, task_id="902")

    result = asyncio.run(
        bundle.mcp.call_tool("kandinsky_task_result", {"task_id": "902", "return_base64": True})
    )

    media = result.structured_content["media"][0]
    raw = base64.b64decode(media["b64"])
    assert raw == PNG
    assert len(raw) == media["bytes"] == record.files[0].bytes
    assert hashlib.sha256(raw).hexdigest() == media["sha256"] == record.files[0].sha256
    assert result.structured_content["next"] == "Файл сохранён. Поле media[].b64 содержит данные файла."


def test_task_result_large_file_reports_error(bundle) -> None:
    """Критерий 3: файл больше 4 МБ — b64_error, остальной ответ штатный."""
    big = b"\x00" * (4 * 1024 * 1024 + 1)
    make_saved_task(bundle, task_id="903", payload=big, mime="image/jpeg", ext=".jpg")

    result = asyncio.run(
        bundle.mcp.call_tool("kandinsky_task_result", {"task_id": "903", "return_base64": True})
    )

    assert result.is_error is False
    media = result.structured_content["media"][0]
    assert media["b64_error"] == "file_too_large"
    assert "b64" not in media
    assert media["bytes"] == len(big)
    assert "url" in media
    assert "4 МБ" in result.structured_content["next"]


def test_task_result_base64_is_repeatable_and_offline(bundle) -> None:
    """Критерий 4: два вызова подряд дают одно и то же, обращений к API нет."""
    make_saved_task(bundle, task_id="904")
    fake = FakeClient()
    bundle.ctx.manager = TaskManager(
        fake,  # type: ignore[arg-type]
        bundle.ctx.store,
        bundle.ctx.registry,
    )

    first = asyncio.run(
        bundle.mcp.call_tool("kandinsky_task_result", {"task_id": "904", "return_base64": True})
    )
    second = asyncio.run(
        bundle.mcp.call_tool("kandinsky_task_result", {"task_id": "904", "return_base64": True})
    )

    assert first.structured_content == second.structured_content
    assert fake.downloads == 0, "работать должны с диском, а не с API"


def test_task_result_not_ready_still_errors(bundle) -> None:
    """Незавершённая задача по-прежнему даёт not_ready, а не b64."""
    from kandinsky_mcp.domain.store import TaskRecord

    bundle.ctx.store.save_task(
        TaskRecord(task_id="905", route="k6-image-t2i", kind="image", prompt="кот", status="processing")
    )

    result = asyncio.run(
        bundle.mcp.call_tool("kandinsky_task_result", {"task_id": "905", "return_base64": True})
    )

    assert result.is_error is True
    assert "not_ready" in result.content[0].text


# --- редактирование картинок из своего хранилища -----------------------------


def test_load_image_accepts_own_results_path(bundle) -> None:
    """Картинку, которую выдал сам сервер, можно передать путём, а не base64."""
    record = make_saved_task(bundle, task_id="910")

    raw = bundle.ctx.load_image(record.files[0].rel_path)

    assert raw == PNG


def test_load_image_accepts_own_signed_url(bundle) -> None:
    record = make_saved_task(bundle, task_id="911")
    url = bundle.ctx.file_url(record.files[0].rel_path)

    raw = bundle.ctx.load_image(url)

    assert raw == PNG


def test_load_image_rejects_foreign_path_with_actionable_hint(bundle) -> None:
    """Путь из сессии агента недоступен — в ошибке должен быть рецепт с base64."""
    with pytest.raises(ValueError) as info:
        bundle.ctx.load_image("/session/cat_dog_sberbank_185.jpg")

    message = str(info.value)
    assert "base64.b64encode" in message
    assert "results/<дата>/<id>.jpg" in message


def test_load_image_does_not_escape_storage(bundle) -> None:
    """Внешние пути через «свои» префиксы не читаем."""
    with pytest.raises(ValueError):
        bundle.ctx.load_image("results/../../etc/passwd")
