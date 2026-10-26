"""Реестр маршрутов Kandinsky API.

Маршрут — это данные, а не код: набор допустимых параметров, значения
перечислений и лимиты входа. Тулы работают через реестр, поэтому добавление
маршрута не требует правок в каждом инструменте.

Реестр сверяется с `GET /openapi.json` при старте (см. `sync_with_openapi`):
два деплоя API различаются набором маршрутов, и жёстко зашитый список
однажды превратился бы в поток 404.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from .errors import NOT_IMPLEMENTED, KandinskyError

# Виды маршрутов
IMAGE = "image"
VIDEO = "video"
VIDEO_AUDIO = "video_audio"
AVATAR = "avatar"

# Единые лимиты API для входных изображений
_IMG_MB = 25.0
_IMG_SIDE = 2048
_SUPERRES_MB = 4.0
_SUPERRES_SIDE = 1024
_PROMPT_MAX = 2048

# Синонимы ключей, которые модель путает: в API ключ называется `aspect_ration`
# (с опечаткой), поэтому `aspect_ratio` надо не отвергать, а переименовать.
_KEY_ALIASES = {"aspect_ratio": "aspect_ration"}


@dataclass(frozen=True, slots=True)
class RouteSpec:
    route: str
    kind: str
    title: str
    requires: tuple[str, ...] = ("query",)
    allowed: frozenset[str] = frozenset()
    enums: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    defaults: Mapping[str, Any] = field(default_factory=dict)
    prompt_max: int = _PROMPT_MAX
    image_max_mb: float = _IMG_MB
    image_max_side: int = _IMG_SIDE
    max_images: int = 3
    output: str = "image/png"
    exposed: bool = True
    available: bool = True

    def supported_params(self) -> tuple[str, ...]:
        return tuple(sorted(self.allowed))

    def mark(self, *, available: bool) -> RouteSpec:
        return replace(self, available=available)


def _image_route(
    route: str,
    title: str,
    *,
    requires: tuple[str, ...] = ("query", "image"),
    allowed: Iterable[str] = ("image", "beautificator", "query"),
    max_images: int = 1,
    exposed: bool = False,
) -> RouteSpec:
    return RouteSpec(
        route=route,
        kind=IMAGE,
        title=title,
        requires=requires,
        allowed=frozenset(allowed),
        enums={"beautificator": ("enabled", "disabled", "gigachat-max")},
        max_images=max_images,
        exposed=exposed,
    )


# --- изображения (в v1 это единственный вид, отдаваемый агентам) --------------

K6_IMAGE_T2I = RouteSpec(
    route="k6-image-t2i",
    kind=IMAGE,
    title="Kandinsky 6 — изображение по тексту",
    requires=("query", "resolution"),
    allowed=frozenset({"query", "resolution", "beautificator"}),
    enums={
        "resolution": ("1024x1024", "768x768", "768x1280", "auto", "1280x768"),
        # Значение "gigachat-max" есть в openapi.json, но воркер его не принимает:
        # «wrong value type for field beatificator: should be Literal instead of
        # value gigachat-max». Три задачи сгорели на этом 24.10.2026, поэтому
        # в реестре оставляем только реально работающие значения.
        "beautificator": ("enabled", "disabled"),
    },
    defaults={"resolution": "1024x1024"},
    exposed=True,
)

K5_IMAGE_T2I = RouteSpec(
    route="k5-image-t2i",
    kind=IMAGE,
    title="Kandinsky 5 — изображение по тексту",
    requires=("query",),
    allowed=frozenset({"query", "resolution", "guidance", "use_original_prompt"}),
    enums={"resolution": ("1024x1024", "768x768", "768x1280", "1280x768", "512x512")},
    defaults={"resolution": "1024x1024", "guidance": 3.5, "use_original_prompt": False},
    exposed=True,
)

K6_I2I = _image_route(
    "k6-i2i",
    "Kandinsky 6 — изображение по картинке и тексту",
    requires=("query", "image"),
    max_images=3,
    exposed=True,
)

K6_KANDISHOP = _image_route(
    "k6-kandishop-quantized",
    "Kandinsky 6 Kandishop — edit по картинке (квантованная модель)",
    requires=("query",),
    max_images=3,
    exposed=True,
)

K6_SUPERRES = RouteSpec(
    route="k6_superres",
    kind=IMAGE,
    title="Kandinsky 6 — увеличение разрешения (super resolution)",
    requires=("image", "upscale"),
    allowed=frozenset({"image", "upscale", "one_step_t"}),
    enums={},
    image_max_mb=_SUPERRES_MB,
    image_max_side=_SUPERRES_SIDE,
    max_images=1,
    exposed=True,
)

# --- видео (задел: записи есть, тулов в v1 нет) -------------------------------

K5_T2V_PRO = RouteSpec(
    route="k5_video_t2v_pro",
    kind=VIDEO,
    title="Kandinsky 5 Pro — видео по тексту",
    requires=("query", "resolution"),
    allowed=frozenset({"query", "resolution", "beautificator"}),
    enums={
        "resolution": ("768x1280", "1280x768"),
        "beautificator": ("enabled", "disabled", "gigachat-max"),
    },
    output="video/mp4",
    exposed=False,
)

K5_T2V_LITE = RouteSpec(
    route="k5_video_t2v_lite",
    kind=VIDEO,
    title="Kandinsky 5 Lite — видео по тексту",
    requires=("query", "resolution"),
    allowed=frozenset({"query", "resolution", "beautificator"}),
    enums={
        "resolution": ("512x512", "512x768", "768x512"),
        "beautificator": ("enabled", "disabled", "gigachat-max"),
    },
    output="video/mp4",
    exposed=False,
)


def _i2v(route: str, title: str) -> RouteSpec:
    return RouteSpec(
        route=route,
        kind=VIDEO,
        title=title,
        requires=("query", "image"),
        allowed=frozenset({"query", "image", "beautificator"}),
        enums={"beautificator": ("enabled", "disabled", "gigachat-max")},
        max_images=1,
        output="video/mp4",
        exposed=False,
    )


K5_I2V_HD = _i2v("k5-i2v-hd", "Kandinsky 5 I2V HD — видео по картинке")
K5_I2V_SD = _i2v("k5-i2v-sd", "Kandinsky 5 I2V SD — видео по картинке")
K5_I2V_LITE = _i2v("k5-i2v-lite", "Kandinsky 5 I2V Lite — видео по картинке")

_PREVIEW_PARAMS = frozenset({"query", "seed", "duration", "rag", "aspect_ration", "images"})

K65_T2VA = RouteSpec(
    route="kandinsky6.5-preview-t2va",
    kind=VIDEO_AUDIO,
    title="Kandinsky 6.5 Preview — видео со звуком по тексту",
    requires=("query",),
    allowed=frozenset({"query", "seed", "duration", "rag", "aspect_ration"}),
    enums={"duration": ("5", "10", "15"), "aspect_ration": ("1:1", "16:9", "9:16")},
    defaults={"duration": 5, "rag": True, "aspect_ration": "16:9"},
    output="video/mp4",
    exposed=False,
)

K65_I2VA = RouteSpec(
    route="kandinsky6.5-preview-i2va",
    kind=VIDEO_AUDIO,
    title="Kandinsky 6.5 Preview — видео со звуком по картинке",
    requires=("query", "images"),
    allowed=_PREVIEW_PARAMS,
    enums={"duration": ("5", "10", "15"), "aspect_ration": ("1:1", "16:9", "9:16")},
    defaults={"duration": 5, "rag": True, "aspect_ration": "16:9"},
    max_images=3,
    output="video/mp4",
    exposed=False,
)

K6_T2VA_LITE = RouteSpec(
    route="k6_t2va_lite",
    kind=VIDEO_AUDIO,
    title="Kandinsky 6 — видео со звуком (Lite)",
    requires=("query",),
    allowed=frozenset({"query", "seed", "resolution", "video_duration", "beautificator"}),
    enums={
        "resolution": ("512x512", "512x768", "768x512"),
        "beautificator": ("enabled", "disabled"),
    },
    defaults={"resolution": "768x512", "video_duration": 5, "seed": 42},
    output="video/mp4",
    exposed=False,
)

GIGA_AVATAR = RouteSpec(
    route="giga_avatar",
    kind=AVATAR,
    title="GigaAvatar — видео-аватар по картинке, аудио и тексту",
    requires=("query", "image", "audio"),
    allowed=frozenset({"query", "image", "audio", "emotion"}),
    enums={"emotion": ("anger_subtle", "happiness_subtle", "sadness_subtle")},
    max_images=1,
    output="video/mp4",
    exposed=False,
)

ALL_ROUTES: tuple[RouteSpec, ...] = (
    K6_IMAGE_T2I,
    K5_IMAGE_T2I,
    K6_I2I,
    K6_KANDISHOP,
    K6_SUPERRES,
    K5_T2V_PRO,
    K5_T2V_LITE,
    K5_I2V_HD,
    K5_I2V_SD,
    K5_I2V_LITE,
    K65_T2VA,
    K65_I2VA,
    K6_T2VA_LITE,
    GIGA_AVATAR,
)


@dataclass(frozen=True, slots=True)
class SyncReport:
    """Результат сверки реестра с OpenAPI-спекой."""

    unavailable: tuple[str, ...] = ()
    unknown: tuple[str, ...] = ()
    enum_mismatches: tuple[str, ...] = ()

    @property
    def clean(self) -> bool:
        return not (self.unavailable or self.unknown or self.enum_mismatches)


class RouteRegistry:
    def __init__(self, routes: Iterable[RouteSpec] = ALL_ROUTES) -> None:
        self._routes: dict[str, RouteSpec] = {r.route: r for r in routes}

    def __contains__(self, route: object) -> bool:
        return route in self._routes

    def all(self) -> tuple[RouteSpec, ...]:
        return tuple(self._routes.values())

    def get(self, route: str) -> RouteSpec:
        spec = self._routes.get(route)
        if spec is None:
            raise KandinskyError(NOT_IMPLEMENTED, f"Маршрут {route!r} не поддерживается")
        return spec

    def by_kind(self, *kinds: str, only_available: bool = False) -> tuple[RouteSpec, ...]:
        result = [r for r in self._routes.values() if not kinds or r.kind in kinds]
        if only_available:
            result = [r for r in result if r.available]
        return tuple(result)

    def exposed(self) -> tuple[RouteSpec, ...]:
        return tuple(r for r in self._routes.values() if r.exposed and r.available)

    def mark_unavailable(self, routes: Iterable[str]) -> None:
        for route in routes:
            if route in self._routes:
                self._routes[route] = self._routes[route].mark(available=False)

    def sync_with_openapi(self, spec: Mapping[str, Any]) -> SyncReport:
        """Сверяет реестр с живой спекой.

        Помечает недоступные маршруты, сообщает о незнакомых и о расхождениях
        в перечислениях. Не падает: сервер должен подниматься даже при
        изменившемся API — просто агент увидит меньше возможностей.
        """
        paths = spec.get("paths") or {}
        live: set[str] = set()
        for path in paths:
            if isinstance(path, str) and path.startswith("/tasks/"):
                tail = path[len("/tasks/") :]
                if tail and "{" not in tail:
                    live.add(tail)

        unavailable = tuple(sorted(r for r in self._routes if r not in live))
        unknown = tuple(sorted(live - set(self._routes)))
        self.mark_unavailable(unavailable)

        mismatches: list[str] = []
        for route in self._routes:
            if route in unavailable:
                continue
            enum = _resolution_enum_for(spec, route)
            expected = self._routes[route].enums.get("resolution")
            if enum and expected and set(enum) != set(expected):
                mismatches.append(
                    f"{route}: resolution {sorted(set(enum) ^ set(expected))} "
                    f"(спек: {sorted(enum)}, реестр: {sorted(expected)})"
                )

        return SyncReport(
            unavailable=unavailable,
            unknown=unknown,
            enum_mismatches=tuple(mismatches),
        )


def _resolution_enum_for(spec: Mapping[str, Any], route: str) -> tuple[str, ...] | None:
    """Достаёт enum resolution строго для этого маршрута.

    Путь: paths[/tasks/<route>].post.requestBody…schema.$ref
           → components.schemas[ref].properties.params.$ref
           → схема параметров модели → properties.resolution.enum
    """
    paths = spec.get("paths") or {}
    operation = (paths.get(f"/tasks/{route}") or {}).get("post") or {}
    content = (operation.get("requestBody") or {}).get("content") or {}
    media = content.get("application/json") or {}
    ref = (media.get("schema") or {}).get("$ref") or ""
    if not ref.startswith("#/components/schemas/"):
        return None

    schemas = (spec.get("components") or {}).get("schemas") or {}
    request_schema = schemas.get(ref.rsplit("/", 1)[-1]) or {}
    params_ref = ((request_schema.get("properties") or {}).get("params") or {}).get("$ref") or ""
    if not params_ref.startswith("#/components/schemas/"):
        return None

    params_schema = schemas.get(params_ref.rsplit("/", 1)[-1]) or {}
    resolution = (params_schema.get("properties") or {}).get("resolution") or {}
    values = resolution.get("enum")
    if not isinstance(values, list):
        return None
    return tuple(sorted(str(v) for v in values))


# --- нормализация параметров --------------------------------------------------

_RESOLUTION_RE = re.compile(r"^\s*(\d{2,5})\s*[:x×*хХ]\s*(\d{2,5})\s*$")


def normalize_resolution(value: str) -> str:
    """Приводит разрешение к виду API: `1024x1024`.

    PDF пишет `"1280:768"`, схема требует `"1280x768"`. Модель будет путать
    разделитель — это должно лечиться, а не падать.
    """
    raw = str(value).strip()
    if raw.lower() == "auto":
        return "auto"
    match = _RESOLUTION_RE.match(raw)
    if not match:
        raise KandinskyError(
            "validation_error",
            f"Не удалось разобрать разрешение {value!r}. Ожидается формат 1024x1024.",
        )
    return f"{int(match.group(1))}x{int(match.group(2))}"


def normalize_params(spec: RouteSpec, params: Mapping[str, Any]) -> dict[str, Any]:
    """Проверяет параметры по реестру и нормализует значения.

    API объявляет `additionalProperties: false`, поэтому лишний ключ — это 422.
    Здесь он отсекается заранее, с понятным сообщением.
    """
    # Синонимы переименовываем до проверки на лишние ключи.
    renamed: dict[str, Any] = {}
    for key, value in params.items():
        renamed[_KEY_ALIASES.get(key, key)] = value

    unknown = sorted(set(renamed) - spec.allowed)
    if unknown:
        raise KandinskyError(
            "validation_error",
            f"Маршрут {spec.route} не принимает параметры: {', '.join(unknown)}. "
            f"Допустимые: {', '.join(spec.supported_params())}",
        )

    cleaned: dict[str, Any] = {}
    for key, value in renamed.items():
        if value is None:
            continue
        if key == "resolution":
            value = normalize_resolution(str(value))
        if key in spec.enums:
            allowed = spec.enums[key]
            candidate = str(value)
            if candidate not in allowed:
                raise KandinskyError(
                    "validation_error",
                    f"{key}={value!r} недопустимо для {spec.route}. "
                    f"Варианты: {', '.join(allowed)}",
                )
            # Тип сохраняем исходный: duration/seed в API — числа, не строки.
            value = int(candidate) if candidate.isdigit() else candidate
        if key == "query":
            text = str(value).strip()
            if not text:
                raise KandinskyError("validation_error", "Пустой промпт")
            if len(text) > spec.prompt_max:
                raise KandinskyError(
                    "prompt_too_long",
                    f"Промпт {len(text)} символов, лимит {spec.prompt_max}. Сократите описание.",
                )
            value = text
        cleaned[key] = value

    missing = [r for r in spec.requires if r not in cleaned]
    if missing:
        raise KandinskyError(
            "validation_error",
            f"Для {spec.route} обязательны: {', '.join(missing)}",
        )
    return cleaned
