"""Работа с Kandinsky API: клиент, ошибки, реестр маршрутов."""

from __future__ import annotations

from .client import DownloadedFile, KandinskyClient, TaskStatus
from .errors import KandinskyError

__all__ = ["DownloadedFile", "KandinskyClient", "KandinskyError", "TaskStatus"]
