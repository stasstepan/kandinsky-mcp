"""Конфигурация: всё из окружения, ничего из кода."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Kandinsky API ---
    kandinsky_base_url: str = "https://kandinskyapi.sberdevices.ru"
    kandinsky_token: SecretStr = SecretStr("")
    http_timeout: float = 30.0
    http_download_timeout: float = 120.0
    http_max_attempts: int = 3

    # --- MCP транспорт ---
    mcp_transport: Literal["http", "stdio"] = "http"
    mcp_host: str = "0.0.0.0"
    mcp_http_port: int = 8000
    mcp_path: str = "/mcp"
    mcp_max_body_bytes: int = 40 * 1024 * 1024  # base64-картинки: +33 % к размеру

    # --- Доступ ---
    mcp_api_key: SecretStr = SecretStr("")
    file_secret: SecretStr = SecretStr("")
    public_base_url: str = "http://127.0.0.1:8000"
    file_url_ttl_seconds: int = 7 * 24 * 3600
    # Лимит на POST-запросы. Значение с запасом: каждый вызов инструмента —
    # это POST, и агент, опрашивающий статус, легко упирался в прежние 30/мин.
    # На живом кейсе агент делал 32 POST в минуту и получал 429, после чего
    # MCP-сессия рвалась с «client session is not running».
    rate_limit_per_minute: int = 120

    # --- Хранилище ---
    data_dir: Path = Path("/data")
    result_ttl_hours: int = 168
    max_inline_bytes: int = 1_500_000

    # --- Поведение задач ---
    route_sync: bool = True
    poll_min_seconds: float = 2.0
    poll_max_seconds: float = 10.0
    poll_concurrency: int = 8
    default_censor: bool = True
    # Сколько секунд инструмент генерации ждёт результат внутри вызова.
    # Держим меньше типичного таймаута MCP-клиента: если ждать дольше, клиент
    # решает, что вызов не удался, и повторяет его — получается вторая задача
    # и двойной расход квоты. Именно так случилось на живой платформе.
    default_wait_seconds: int = 25
    # Окно защиты от таких повторов: одинаковый запрос в этом интервале
    # возвращает уже созданную задачу вместо новой.
    dedupe_window_seconds: int = 90
    # Читать входные картинки с диска сервера. В HTTP-режиме это лишняя
    # поверхность (чтение произвольных файлов), поэтому включается только в stdio.
    allow_local_paths: bool = False

    @field_validator("kandinsky_base_url", "public_base_url")
    @classmethod
    def _strip_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @field_validator("data_dir")
    @classmethod
    def _absolute(cls, value: Path) -> Path:
        return value.expanduser()

    @property
    def token(self) -> str:
        return self.kandinsky_token.get_secret_value()

    @property
    def api_key(self) -> str:
        return self.mcp_api_key.get_secret_value()

    @property
    def signing_key(self) -> str:
        # Для подписи ссылок годится отдельный секрет; если не задан — берём API-ключ.
        return self.file_secret.get_secret_value() or self.api_key

    @property
    def auth_enabled(self) -> bool:
        return bool(self.api_key)

    @property
    def results_dir(self) -> Path:
        return self.data_dir / "results"

    @property
    def tasks_dir(self) -> Path:
        return self.data_dir / "tasks"

    def require_token(self) -> str:
        if not self.token:
            raise RuntimeError(
                "KANDINSKY_TOKEN не задан. Укажите Bearer-токен Kandinsky API "
                "в переменной окружения KANDINSKY_TOKEN (или в .env)."
            )
        return self.token

    def validate_for_http(self) -> None:
        self.require_token()
        if not self.auth_enabled:
            raise RuntimeError(
                "MCP_API_KEY не задан: HTTP-транспорт без ключа доступа не поднимаем. "
                "Задайте MCP_API_KEY (и передавайте его в заголовке X-API-Key)."
            )


__all__ = ["Settings"]
