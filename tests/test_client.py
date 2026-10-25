"""Клиент API: контракт с Kandinsky, ретраи, отсутствие дубликатов задач."""

from __future__ import annotations

import httpx
import pytest
import respx

from kandinsky_mcp.api import errors as e
from kandinsky_mcp.api.client import KandinskyClient

BASE = "https://kandinsky.test"


@pytest.fixture
async def client():
    async with KandinskyClient(BASE, "test-token", max_attempts=3) as c:
        yield c


@respx.mock
async def test_create_task_sends_bearer_and_body(client: KandinskyClient) -> None:
    route = respx.post(f"{BASE}/tasks/k6-image-t2i").mock(
        return_value=httpx.Response(200, json={"task_id": "1842"})
    )

    task_id = await client.create_task(
        "k6-image-t2i",
        {"query": "закат над морем", "resolution": "1024x1024"},
        censor=False,
    )

    assert task_id == "1842"
    request = route.calls.last.request
    assert request.headers["Authorization"] == "Bearer test-token"
    import json

    assert json.loads(request.content) == {
        "censor": False,
        "params": {"query": "закат над морем", "resolution": "1024x1024"},
    }


@respx.mock
async def test_create_task_omits_censor_when_none(client: KandinskyClient) -> None:
    """censor=None → ключ не отправляем, сервер применит своё умолчание."""
    route = respx.post(f"{BASE}/tasks/k6-image-t2i").mock(
        return_value=httpx.Response(200, json={"task_id": "7"})
    )

    await client.create_task("k6-image-t2i", {"query": "кот", "resolution": "1024x1024"})

    import json

    body = json.loads(route.calls.last.request.content)
    assert "censor" not in body


@respx.mock
async def test_create_task_without_task_id_in_response_is_an_error(client: KandinskyClient) -> None:
    respx.post(f"{BASE}/tasks/k6-image-t2i").mock(return_value=httpx.Response(200, json={"ok": True}))

    with pytest.raises(e.KandinskyError) as info:
        await client.create_task("k6-image-t2i", {"query": "кот"})
    assert info.value.code == e.UPSTREAM_ERROR


@respx.mock
async def test_status_202_is_processing(client: KandinskyClient) -> None:
    respx.get(f"{BASE}/tasks/42").mock(return_value=httpx.Response(202, json={"status": "processing"}))

    status = await client.get_status("42")

    assert status.status == "processing"
    assert status.http_status == 202
    assert status.is_terminal is False


@respx.mock
async def test_status_200_failed_carries_error(client: KandinskyClient) -> None:
    respx.get(f"{BASE}/tasks/42").mock(
        return_value=httpx.Response(
            200,
            json={"status": "failed", "error": "Task failed due to generation timeout (45 minutes)"},
        )
    )

    status = await client.get_status("42")

    assert status.is_failed is True
    assert "generation timeout" in (status.error or "")


@respx.mock
async def test_download_result_returns_binary_and_extension(client: KandinskyClient) -> None:
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 32
    respx.get(f"{BASE}/tasks/42/result").mock(
        return_value=httpx.Response(
            200, content=png, headers={"content-type": "image/png; charset=binary"}
        )
    )

    media = await client.download_result("42")

    assert media.content == png
    assert media.content_type == "image/png"
    assert media.extension == ".png"
    assert media.size == len(png)


@respx.mock
async def test_download_video_extension(client: KandinskyClient) -> None:
    respx.get(f"{BASE}/tasks/42/result").mock(
        return_value=httpx.Response(200, content=b"ftyp", headers={"content-type": "video/mp4"})
    )
    media = await client.download_result("42")
    assert media.extension == ".mp4"


# --- формат определяется по байтам, а не по заголовку -------------------------
# Живой прогон показал: API отдаёт JPEG с Content-Type: image/png.

JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01" + b"0" * 24
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 24


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (PNG, "image/png"),
        (JPEG, "image/jpeg"),
        (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "image/webp"),
        (b"RIFF\x00\x00\x00\x00WAVEfmt ", "audio/wav"),
        (b"\x00\x00\x00\x18ftypmp42", "video/mp4"),
        (b"garbage-bytes", None),
    ],
)
def test_sniff_mime(content: bytes, expected: str | None) -> None:
    from kandinsky_mcp.api.client import sniff_mime

    assert sniff_mime(content) == expected


@respx.mock
async def test_jpeg_with_png_header_is_saved_as_jpeg(client: KandinskyClient) -> None:
    """Иначе файл получит расширение .png, и клиент откажется его рисовать."""
    respx.get(f"{BASE}/tasks/42/result").mock(
        return_value=httpx.Response(200, content=JPEG, headers={"content-type": "image/png"})
    )

    media = await client.download_result("42")

    assert media.content_type == "image/jpeg"
    assert media.extension == ".jpg"


@respx.mock
async def test_declared_mime_is_used_when_bytes_are_unknown(client: KandinskyClient) -> None:
    respx.get(f"{BASE}/tasks/42/result").mock(
        return_value=httpx.Response(200, content=b"\x00\x01\x02", headers={"content-type": "image/png"})
    )
    media = await client.download_result("42")
    assert media.content_type == "image/png"


@respx.mock
async def test_quota_429_is_mapped_and_not_retried(client: KandinskyClient) -> None:
    route = respx.post(f"{BASE}/tasks/k6-image-t2i").mock(
        return_value=httpx.Response(
            429, json={"detail": {"code": "LIMIT_EXHAUSTED", "type": "daily"}}
        )
    )

    with pytest.raises(e.KandinskyError) as info:
        await client.create_task("k6-image-t2i", {"query": "кот"})

    assert info.value.code == e.QUOTA_DAILY
    assert route.call_count == 1  # квоту не жжём повторами


@respx.mock
async def test_gateway_error_is_retried_then_succeeds(client: KandinskyClient) -> None:
    route = respx.get(f"{BASE}/tasks/42").mock(
        side_effect=[
            httpx.Response(503, text="upstream busy"),
            httpx.Response(200, json={"status": "done"}),
        ]
    )

    status = await client.get_status("42")

    assert status.status == "done"
    assert route.call_count == 2


@respx.mock
async def test_network_error_becomes_domain_error(client: KandinskyClient) -> None:
    respx.get(f"{BASE}/tasks/42").mock(side_effect=httpx.ConnectError("нет сети"))

    with pytest.raises(e.KandinskyError) as info:
        await client.get_status("42")

    assert info.value.code == e.NETWORK_ERROR


@respx.mock
async def test_health_returns_false_instead_of_raising(client: KandinskyClient) -> None:
    respx.get(f"{BASE}/health").mock(return_value=httpx.Response(503, json={"status": "down"}))
    assert await client.health() is False

    respx.get(f"{BASE}/health").mock(return_value=httpx.Response(200, json={"status": "ok"}))
    assert await client.health() is True


@respx.mock
async def test_404_on_status_marks_not_found(client: KandinskyClient) -> None:
    respx.get(f"{BASE}/tasks/999").mock(return_value=httpx.Response(404, json={"detail": "Task not found"}))
    with pytest.raises(e.KandinskyError) as info:
        await client.get_status("999")
    assert info.value.code == e.NOT_FOUND
