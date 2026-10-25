"""Подготовка входных изображений под лимиты Kandinsky API.

API ограничивает вход по двум осям сразу: размер файла (25 МБ, у superres 4 МБ)
и сторона картинки (2048 px, у superres 1024 px). Если отправить как есть —
получишь 413/422 уже после траты квоты. Поэтому проверяем и чиним заранее.
"""

from __future__ import annotations

import base64
import io
from dataclasses import dataclass

from PIL import Image, UnidentifiedImageError

from ..api.errors import INPUT_BAD_RESOLUTION, INPUT_TOO_LARGE, KandinskyError

# JPEG-качество при пережатии: 85 — компромисс между весом и артефактами.
_JPEG_QUALITY = 85


@dataclass(slots=True)
class PreparedImage:
    data: bytes
    mime: str
    width: int
    height: int
    resized: bool = False
    recompressed: bool = False

    @property
    def size(self) -> int:
        return len(self.data)

    @property
    def base64(self) -> str:
        return base64.b64encode(self.data).decode("ascii")

    def describe(self) -> str:
        parts = [f"{self.width}x{self.height}", f"{self.size / 1024:.0f} КБ", self.mime]
        if self.resized:
            parts.append("уменьшено до лимита")
        if self.recompressed:
            parts.append("пережато")
        return ", ".join(parts)


def prepare_image(
    data: bytes,
    *,
    max_mb: float,
    max_side: int,
) -> PreparedImage:
    """Проверяет картинку и при необходимости приводит её к лимитам.

    Маленькие картинки не растягиваем: это не улучшает результат пейзажа,
    только тратит время на передачу.
    """
    if not data:
        raise KandinskyError(INPUT_TOO_LARGE, "Пустой файл изображения")

    try:
        with Image.open(io.BytesIO(data)) as probe:
            probe.load()
            width, height = probe.size
            fmt = (probe.format or "").upper()
            image = probe.copy()
    except (UnidentifiedImageError, OSError) as exc:
        raise KandinskyError(
            INPUT_TOO_LARGE,
            f"Не удалось прочитать изображение: {exc}. Поддерживаются JPEG и PNG.",
        ) from exc

    if width <= 0 or height <= 0:
        raise KandinskyError(INPUT_BAD_RESOLUTION, f"Некорректные размеры изображения: {width}x{height}")

    resized = False
    longest = max(width, height)
    if longest > max_side:
        scale = max_side / longest
        new_size = (max(1, int(width * scale)), max(1, int(height * scale)))
        image = image.resize(new_size, Image.Resampling.LANCZOS)
        width, height = image.size
        resized = True

    # PNG оставляем PNG (альфа-канал), остальное переводим в JPEG.
    keep_png = fmt == "PNG" or image.mode in {"RGBA", "LA", "P"}
    mime = "image/png" if keep_png else "image/jpeg"
    if keep_png and image.mode not in {"RGB", "RGBA"}:
        image = image.convert("RGBA")
    elif not keep_png and image.mode != "RGB":
        image = image.convert("RGB")

    data, recompressed = _encode(image, mime=mime, max_bytes=int(max_mb * 1024 * 1024))
    if len(data) > max_mb * 1024 * 1024:
        raise KandinskyError(
            INPUT_TOO_LARGE,
            f"Изображение {len(data) / 1024 / 1024:.1f} МБ не удалось уложить в лимит {max_mb} МБ.",
        )

    return PreparedImage(
        data=data,
        mime=mime,
        width=width,
        height=height,
        resized=resized,
        recompressed=recompressed,
    )


def _encode(image: Image.Image, *, mime: str, max_bytes: int) -> tuple[bytes, bool]:
    """Кодирует картинку, снижая качество, пока не уложится в лимит.

    Второй элемент — признак «файл был пережат заново» (для честного отчёта агенту).
    """
    if mime == "image/png":
        buffer = io.BytesIO()
        image.save(buffer, format="PNG", optimize=True)
        return buffer.getvalue(), False

    quality = _JPEG_QUALITY
    raw = b""
    while quality >= 40:
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=quality, optimize=True)
        raw = buffer.getvalue()
        if len(raw) <= max_bytes:
            break
        quality -= 15
    return raw, True
