"""Подготовка входных картинок: лимиты сторон и веса."""

from __future__ import annotations

import io

import pytest
from PIL import Image

from kandinsky_mcp.api import errors as e
from kandinsky_mcp.domain.media import prepare_image


def make_image(width: int, height: int, fmt: str = "PNG", mode: str = "RGB") -> bytes:
    buffer = io.BytesIO()
    Image.new(mode, (width, height), color="purple").save(buffer, format=fmt)
    return buffer.getvalue()


def test_small_image_passes_untouched() -> None:
    raw = make_image(512, 512)

    prepared = prepare_image(raw, max_mb=25, max_side=2048)

    assert prepared.width == 512
    assert prepared.height == 512
    assert prepared.resized is False
    assert prepared.mime == "image/png"


def test_oversized_side_is_downscaled_preserving_aspect() -> None:
    raw = make_image(3000, 2000)

    prepared = prepare_image(raw, max_mb=25, max_side=2048)

    assert prepared.resized is True
    assert max(prepared.width, prepared.height) == 2048
    # Пропорции сохранены (2000/3000 = 0.666…)
    assert abs(prepared.width / prepared.height - 1.5) < 0.01


def test_superres_limits_are_stricter() -> None:
    raw = make_image(2000, 2000)

    prepared = prepare_image(raw, max_mb=4, max_side=1024)

    assert max(prepared.width, prepared.height) == 1024
    assert prepared.resized is True


def test_jpeg_stays_jpeg() -> None:
    raw = make_image(800, 600, fmt="JPEG")

    prepared = prepare_image(raw, max_mb=25, max_side=2048)

    assert prepared.mime == "image/jpeg"


def test_rgba_png_keeps_alpha_and_png() -> None:
    raw = make_image(256, 256, fmt="PNG", mode="RGBA")

    prepared = prepare_image(raw, max_mb=25, max_side=2048)

    assert prepared.mime == "image/png"


def test_garbage_bytes_are_reported_clearly() -> None:
    with pytest.raises(e.KandinskyError) as info:
        prepare_image(b"not an image at all", max_mb=25, max_side=2048)
    assert "JPEG и PNG" in info.value.message


def test_empty_bytes_are_rejected() -> None:
    with pytest.raises(e.KandinskyError):
        prepare_image(b"", max_mb=25, max_side=2048)


def test_base64_roundtrip_matches_payload() -> None:
    import base64

    raw = make_image(64, 64)
    prepared = prepare_image(raw, max_mb=25, max_side=2048)

    assert base64.b64decode(prepared.base64) == prepared.data


def test_describe_is_human_readable() -> None:
    prepared = prepare_image(make_image(3000, 2000), max_mb=25, max_side=2048)
    text = prepared.describe()
    assert "2048x1365" in text
    assert "КБ" in text
    assert "уменьшено" in text
