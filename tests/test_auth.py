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


def test_rate_limit_returns_retry_after() -> None:
    """429 без Retry-After ломает MCP-сессию: клиент считает её мёртвой.

    На живом кейсе агент сделал 32 POST в минуту, получил 429 и потерял
    сессию с «client session is not running».
    """
    import asyncio

    import httpx
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    from kandinsky_mcp.auth import ApiKeyMiddleware, RateLimiter, RateLimitMiddleware

    async def endpoint(request):
        return JSONResponse({"ok": True})

    inner = Starlette(routes=[Route("/mcp", endpoint, methods=["POST"])])
    limited = RateLimitMiddleware(inner, limiter=RateLimiter(2))
    app = ApiKeyMiddleware(limited, api_key=KEY)

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
            return [await c.post("/mcp", headers={"X-API-Key": KEY}) for _ in range(3)]

    first, second, third = asyncio.run(run())
    assert first.status_code == 200
    assert second.status_code == 200
    assert third.status_code == 429
    assert third.headers["retry-after"] == "5"
    assert "rate_limited" in third.text


def test_rate_limit_default_allows_agent_polling() -> None:
    """Умолчание должно быть с запасом: агент опрашивает статус десятками запросов."""
    from kandinsky_mcp.config import Settings

    assert Settings.model_fields["rate_limit_per_minute"].default >= 100
