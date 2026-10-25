"""Авторизация: ключ принимается в нескольких формах.

Отдельный тест-файл, потому что здесь легко случайно ослабить проверку:
важно, чтобы «ключ в любом заголовке» не превратилось в «пускать всех».
"""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from kandinsky_mcp.auth import _candidates, _matches, sign_file, verify_file

KEY = "sk-0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"  # синтетический: формат как у настоящего, значение вымышленное
BARE = KEY[3:]


@pytest.mark.parametrize(
    "value",
    [
        KEY,
        BARE,
        f"Bearer {KEY}",
        f"bearer {KEY}",
        f"  {KEY}  ",
        f"Bearer {BARE}",
    ],
)
def test_matches_accepts_expected_forms(value: str) -> None:
    assert _matches(value, KEY) is True


@pytest.mark.parametrize("value", ["", "wrong", "sk-", "Bearer ", KEY + "x", KEY[:-1]])
def test_matches_rejects_everything_else(value: str) -> None:
    assert _matches(value, KEY) is False


def test_candidates_expands_prefix_and_scheme() -> None:
    assert BARE in _candidates(KEY)
    assert KEY in _candidates(f"Bearer {KEY}")


# --- поведение middleware целиком, через реальный ASGI-запрос ----------------


def make_app():
    from kandinsky_mcp.auth import ApiKeyMiddleware

    async def endpoint(request):
        return JSONResponse({"ok": True})

    inner = Starlette(
        routes=[
            Route("/mcp", endpoint, methods=["POST", "GET"]),
            Route("/healthz", endpoint),
            Route("/files/{p:path}", endpoint),
        ]
    )
    return ApiKeyMiddleware(inner, api_key=KEY)


@pytest.fixture
def client():
    import httpx

    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=make_app()),
        base_url="http://testserver",
    )


async def test_x_api_key_passes(client) -> None:
    async with client as c:
        r = await c.post("/mcp", headers={"X-API-Key": KEY})
    assert r.status_code == 200


async def test_bearer_passes(client) -> None:
    async with client as c:
        r = await c.post("/mcp", headers={"Authorization": f"Bearer {KEY}"})
    assert r.status_code == 200


async def test_key_in_header_name_passes(client) -> None:
    """Реальный случай: платформа подставила ключ в имя заголовка."""
    async with client as c:
        r = await c.post("/mcp", headers={f"Sk-{BARE}": BARE})
    assert r.status_code == 200


async def test_key_in_header_value_passes(client) -> None:
    async with client as c:
        r = await c.post("/mcp", headers={"X-Tenant-Id": f"Bearer {KEY}"})
    assert r.status_code == 200


async def test_no_key_is_rejected(client) -> None:
    async with client as c:
        r = await c.post("/mcp")
    assert r.status_code == 401
    assert "unauthorized" in r.text


async def test_wrong_key_is_rejected(client) -> None:
    async with client as c:
        r = await c.post("/mcp", headers={"X-API-Key": "wrong"})
    assert r.status_code == 401


async def test_health_and_files_are_exempt(client) -> None:
    async with client as c:
        assert (await c.get("/healthz")).status_code == 200
        assert (await c.get("/files/a/b.png")).status_code == 200


async def test_cookie_alone_does_not_authorize(client) -> None:
    """Служебные заголовки не участвуют в поиске ключа."""
    async with client as c:
        r = await c.post("/mcp", headers={"Cookie": f"key={KEY}"})
    assert r.status_code == 401


# --- подпись ссылок на файлы -------------------------------------------------


def test_signed_url_roundtrip() -> None:
    query, expires = sign_file("secret", "results/2026-01-01/1.png", 600)
    sig = query.split("sig=")[1]
    assert verify_file("secret", "results/2026-01-01/1.png", str(expires), sig) is True


def test_signature_bound_to_path() -> None:
    query, expires = sign_file("secret", "results/a.png", 600)
    sig = query.split("sig=")[1]
    assert verify_file("secret", "results/b.png", str(expires), sig) is False


def test_expired_signature_rejected() -> None:
    query, _ = sign_file("secret", "results/a.png", 1)
    sig = query.split("sig=")[1]
    assert verify_file("secret", "results/a.png", "1", sig) is False
