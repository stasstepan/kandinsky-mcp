"""Справочные инструменты: что вообще доступно на этом хосте API."""

from __future__ import annotations

from typing import Annotated

from mcp.server import MCPServer
from mcp.types import CallToolResult
from pydantic import Field

from ..api.routes import AVATAR, IMAGE, VIDEO, VIDEO_AUDIO
from .context import AppContext, text_result


def register_info_tools(mcp: MCPServer, ctx: AppContext) -> None:
    @mcp.tool(
        name="kandinsky_routes",
        title="Доступные модели и параметры",
        description=(
            "Справка по моделям: какие маршруты доступны, какие параметры они "
            "принимают, допустимые значения и лимиты.\n"
            "Reference of available routes: parameters, allowed values and limits.\n\n"
            "Вызывайте первым, если не уверены, какая модель и какие значения подходят. "
            "Список сверяется с OpenAPI-спекой хоста при старте."
        ),
    )
    async def kandinsky_routes(
        kind: Annotated[
            str | None,
            Field(description="Фильтр: image, video, video_audio, avatar. / Filter by kind."),
        ] = None,
        available_only: Annotated[
            bool, Field(description="Только реально доступные на этом хосте. / Only callable routes.")
        ] = True,
        exposed_only: Annotated[
            bool,
            Field(description="Только те, под которые есть инструменты. / Only routes with tools."),
        ] = True,
    ) -> CallToolResult:
        kinds = (kind,) if kind else ()
        specs = ctx.registry.by_kind(*kinds, only_available=available_only)
        if exposed_only:
            specs = tuple(s for s in specs if s.exposed)

        routes = []
        for spec in specs:
            routes.append(
                {
                    "route": spec.route,
                    "kind": spec.kind,
                    "title": spec.title,
                    "tool": _tool_for(spec.route),
                    "required_params": list(spec.requires),
                    "optional_params": sorted(spec.allowed - set(spec.requires)),
                    "enums": {k: list(v) for k, v in spec.enums.items()},
                    "defaults": spec.defaults,
                    "limits": {
                        "prompt_max_chars": spec.prompt_max,
                        "input_image_max_mb": spec.image_max_mb,
                        "input_image_max_side_px": spec.image_max_side,
                        "max_input_images": spec.max_images,
                    },
                    "output": spec.output,
                }
            )

        payload = {
            "api_host": ctx.settings.kandinsky_base_url,
            "count": len(routes),
            "kinds_available": sorted({s.kind for s in ctx.registry.by_kind(only_available=True)}),
            "kinds_all": [IMAGE, VIDEO, VIDEO_AUDIO, AVATAR],
            "routes": routes,
            "note": (
                "Видео, видео со звуком и аватар есть в API, но инструментов под них "
                "в этой версии нет — они появятся в следующей итерации."
            ),
        }
        return text_result(payload)


def _tool_for(route: str) -> str | None:
    return {
        "k6-image-t2i": "kandinsky_image_generate (model=k6)",
        "k5-image-t2i": "kandinsky_image_generate (model=k5)",
        "k6-i2i": "kandinsky_image_edit (model=k6)",
        "k6-kandishop-quantized": "kandinsky_image_edit (model=kandishop)",
        "k6_superres": "kandinsky_image_upscale",
    }.get(route)
