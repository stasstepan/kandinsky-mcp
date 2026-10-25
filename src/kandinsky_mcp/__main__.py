"""Точка входа: выбирает транспорт по MCP_TRANSPORT.

    python -m kandinsky_mcp                 # HTTP (по умолчанию)
    MCP_TRANSPORT=stdio python -m kandinsky_mcp
"""

from __future__ import annotations

import logging
import os
import sys

from .config import Settings
from .server import build, configure_logging

log = logging.getLogger("kandinsky_mcp")


def main(argv: list[str] | None = None) -> int:
    del argv  # аргументов командной строки нет: всё через окружение

    configure_logging(os.environ.get("LOG_LEVEL", "INFO"))

    try:
        bundle = build()
    except RuntimeError as exc:
        log.error("не удалось запустить сервер: %s", exc)
        return 2

    settings: Settings = bundle.settings

    if settings.mcp_transport == "stdio":
        log.info("запуск в режиме stdio")
        bundle.mcp.run(transport="stdio")
        return 0

    import uvicorn

    log.info(
        "запуск HTTP на %s:%s%s (API: %s)",
        settings.mcp_host,
        settings.mcp_http_port,
        settings.mcp_path,
        settings.kandinsky_base_url,
    )
    uvicorn.run(
        bundle.app,
        host=settings.mcp_host,
        port=settings.mcp_http_port,
        log_level=os.environ.get("LOG_LEVEL", "info").lower(),
        access_log=False,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
