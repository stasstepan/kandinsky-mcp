"""Общие фикстуры тестов.

Внимание: встроенная фикстура `tmp_path` в этой песочнице не работает —
`tempfile.mkdtemp` создаёт каталоги без права записи. Поэтому используется
собственная `workdir`, которая создаёт каталог через `Path.mkdir`.
"""

from __future__ import annotations

import shutil
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture
def workdir() -> Iterator[Path]:
    base = Path(__file__).parent / "_work" / uuid.uuid4().hex[:8]
    base.mkdir(parents=True, exist_ok=False)
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)
