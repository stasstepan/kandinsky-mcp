"""Реестр маршрутов: сверка с OpenAPI и нормализация параметров."""

from __future__ import annotations

from typing import Any

import pytest

from kandinsky_mcp.api import errors as e
from kandinsky_mcp.api.routes import (
    IMAGE,
    K6_IMAGE_T2I,
    K6_SUPERRES,
    RouteRegistry,
    normalize_params,
    normalize_resolution,
)


def make_spec(routes: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """Минимальная спека в форме FastAPI: путь + ссылка на схему параметров."""
    routes = routes or {}
    paths: dict[str, Any] = {}
    schemas: dict[str, Any] = {}
    for i, (route, resolution_enum) in enumerate(routes.items()):
        params_name = f"TParams{i}"
        request_name = f"CreateReq{i}"
        schemas[params_name] = {
            "type": "object",
            "properties": {"resolution": {"enum": resolution_enum}} if resolution_enum else {},
        }
        schemas[request_name] = {
            "type": "object",
            "properties": {"params": {"$ref": f"#/components/schemas/{params_name}"}},
        }
        paths[f"/tasks/{route}"] = {
            "post": {
                "requestBody": {
                    "content": {"application/json": {"schema": {"$ref": f"#/components/schemas/{request_name}"}}}
                }
            }
        }
    return {"openapi": "3.1.0", "paths": paths, "components": {"schemas": schemas}}


def test_get_unknown_route_raises_not_implemented() -> None:
    registry = RouteRegistry()
    with pytest.raises(e.KandinskyError) as info:
        registry.get("nope")
    assert info.value.code == e.NOT_IMPLEMENTED


def test_exposed_routes_are_images_only_in_v1() -> None:
    exposed = RouteRegistry().exposed()
    assert exposed, "должен быть хотя бы один инструмент"
    assert all(r.kind == IMAGE for r in exposed)
    names = {r.route for r in exposed}
    assert names == {"k6-image-t2i", "k5-image-t2i", "k6-i2i", "k6-kandishop-quantized", "k6_superres"}


def test_video_routes_are_registered_but_not_exposed() -> None:
    """Задел на итерацию 2: маршруты есть в реестре, тулов под них нет."""
    routes = {r.route: r for r in RouteRegistry().all()}
    assert "k5_video_t2v_pro" in routes
    assert routes["k5_video_t2v_pro"].exposed is False


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1024:1024", "1024x1024"),
        ("1024x1024", "1024x1024"),
        ("1024×768", "1024x768"),
        ("1024 х 768", "1024x768"),
        (" 768*1280 ", "768x1280"),
        ("auto", "auto"),
        ("AUTO", "auto"),
    ],
)
def test_normalize_resolution_accepts_common_separators(raw: str, expected: str) -> None:
    assert normalize_resolution(raw) == expected


def test_normalize_resolution_rejects_garbage() -> None:
    with pytest.raises(e.KandinskyError) as info:
        normalize_resolution("большая")
    assert info.value.message.startswith("Не удалось разобрать")


def test_normalize_params_rejects_unknown_key_with_hint() -> None:
    with pytest.raises(e.KandinskyError) as info:
        normalize_params(K6_IMAGE_T2I, {"query": "кот", "resolution": "1024x1024", "style": "anime"})
    assert "style" in info.value.message
    assert "resolution" in info.value.message  # подсказка со списком допустимых


def test_normalize_params_rejects_bad_enum_and_lists_options() -> None:
    with pytest.raises(e.KandinskyError) as info:
        normalize_params(K6_IMAGE_T2I, {"query": "кот", "resolution": "1024x768"})
    assert "resolution" in info.value.message
    assert "1024x1024" in info.value.message


def test_normalize_params_enforces_prompt_limit() -> None:
    with pytest.raises(e.KandinskyError) as info:
        normalize_params(K6_IMAGE_T2I, {"query": "я" * 3000, "resolution": "1024x1024"})
    assert info.value.code == e.PROMPT_TOO_LONG
    assert "3000" in info.value.message


def test_normalize_params_reports_missing_required() -> None:
    with pytest.raises(e.KandinskyError) as info:
        normalize_params(K6_IMAGE_T2I, {"query": "кот"})  # нет resolution
    assert "resolution" in info.value.message


def test_normalize_params_accepts_colon_resolution_from_pdf() -> None:
    """В PDF написано 1280:768 — модель повторит, сервер обязан починить."""
    cleaned = normalize_params(K6_IMAGE_T2I, {"query": "кот", "resolution": "1280:768"})
    assert cleaned["resolution"] == "1280x768"


def test_normalize_params_drops_none_values() -> None:
    cleaned = normalize_params(
        K6_IMAGE_T2I, {"query": "кот", "resolution": "1024x1024", "beautificator": None}
    )
    assert "beautificator" not in cleaned


def test_superres_has_stricter_image_limits() -> None:
    assert K6_SUPERRES.image_max_mb == 4.0
    assert K6_SUPERRES.image_max_side == 1024
    assert K6_IMAGE_T2I.image_max_side == 2048


def test_sync_marks_missing_routes_unavailable() -> None:
    """Деплой без части маршрутов (как kandinskyapi.ru) не должен ронять сервер."""
    spec = make_spec({"k6-image-t2i": ["1024x1024"], "k6-i2i": None})
    registry = RouteRegistry()

    report = registry.sync_with_openapi(spec)

    assert "k6_superres" in report.unavailable
    assert "k6-image-t2i" not in report.unavailable
    assert registry.get("k6_superres").available is False
    assert all(r.route != "k6_superres" for r in registry.exposed())


def test_sync_reports_unknown_routes_from_api() -> None:
    spec = make_spec({"k6-image-t2i": ["1024x1024"], "brand-new-route": None})
    report = RouteRegistry().sync_with_openapi(spec)
    assert report.unknown == ("brand-new-route",)


def test_sync_detects_resolution_enum_mismatch() -> None:
    spec = make_spec({"k6-image-t2i": ["1024x1024", "2048x2048"]})
    report = RouteRegistry().sync_with_openapi(spec)
    assert report.enum_mismatches
    assert "k6-image-t2i" in report.enum_mismatches[0]


def test_sync_tolerates_route_without_resolution() -> None:
    """У k6-i2i нет resolution — расхождением это быть не должно."""
    spec = make_spec({"k6-image-t2i": ["1024x1024", "768x768", "768x1280", "auto", "1280x768"]})
    report = RouteRegistry().sync_with_openapi(spec)
    assert not any("k6-i2i" in m for m in report.enum_mismatches)


def test_sync_marks_route_unavailable_when_absent_from_spec() -> None:
    registry = RouteRegistry()
    registry.sync_with_openapi({"paths": {}})
    assert all(not r.available for r in registry.all())
    assert registry.exposed() == ()
