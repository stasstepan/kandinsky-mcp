"""Живая проверка: одна настоящая генерация через тот же код, что и MCP-тулы.

Запуск в контейнере (самый простой способ убедиться, что всё работает):

    docker compose exec kandinsky-mcp kandinsky-mcp-smoke
    docker compose exec kandinsky-mcp kandinsky-mcp-smoke "кот в шляпе" 1280x768

Локально:

    KANDINSKY_TOKEN=... python -m kandinsky_mcp.smoke

Тратит одну генерацию из квоты. Проверяет весь путь целиком: сверку реестра с
OpenAPI, создание задачи, поллинг, однократное скачивание и запись файла.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

from .api.client import KandinskyClient
from .api.errors import KandinskyError
from .api.routes import RouteRegistry, normalize_params
from .config import Settings
from .domain.store import FileStore
from .domain.tasks import TaskManager

DEFAULT_PROMPT = "рыжий кот на подоконнике, утренний свет, акварель"
DEFAULT_RESOLUTION = "1024x1024"
MAX_WAIT_SECONDS = 360


async def run(prompt: str, resolution: str) -> int:
    settings = Settings()
    token = settings.token
    if not token:
        print("KANDINSKY_TOKEN не задан — живая проверка невозможна", file=sys.stderr)
        return 2

    registry = RouteRegistry()
    store = FileStore(Path(settings.data_dir))
    client = KandinskyClient(settings.kandinsky_base_url, token)

    try:
        print(f"API:    {settings.kandinsky_base_url}")
        print(f"health: {'ok' if await client.health() else 'НЕДОСТУПЕН'}")

        print("сверка реестра с openapi.json…")
        try:
            report = registry.sync_with_openapi(await client.fetch_openapi())
            print(f"  недоступны на хосте: {', '.join(report.unavailable) or '—'}")
            print(f"  незнакомые маршруты: {', '.join(report.unknown) or '—'}")
            print(f"  расхождения enum:    {', '.join(report.enum_mismatches) or '—'}")
        except KandinskyError as exc:
            print(f"  не удалось: {exc.to_agent_text()}")

        spec = registry.get("k6-image-t2i")
        if not spec.available:
            print("маршрут k6-image-t2i недоступен на этом хосте", file=sys.stderr)
            return 3

        params = normalize_params(spec, {"query": prompt, "resolution": resolution})
        manager = TaskManager(client, store, registry, poll_min=2.0, poll_max=8.0)
        started = time.monotonic()

        print(f"\nсоздаю задачу: {prompt!r} @ {params['resolution']}")
        record = await manager.create(
            spec.route, params, censor=settings.default_censor, prompt=prompt
        )
        print(f"task_id={record.task_id}, ждём результат…")

        deadline = time.monotonic() + MAX_WAIT_SECONDS
        while time.monotonic() < deadline:
            record = store.require_task(record.task_id)
            if record.is_terminal:
                break
            await asyncio.sleep(3)

        elapsed = time.monotonic() - started
        print(f"\nитог: status={record.status} за {elapsed:.1f} с, опросов={record.polls}")

        if record.status != "saved":
            print(f"  ошибка: {record.error}", file=sys.stderr)
            return 1

        for stored in record.files:
            path = store.resolve(stored.rel_path)
            print(f"  файл:    {path}")
            print(f"  формат:  {stored.mime} ({stored.bytes} байт)")
            print(f"  sha256:  {stored.sha256}")
            print(f"  на диске: {path.exists()}")
        await manager.close()
        return 0
    finally:
        await client.close()


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    prompt = args[0] if args else DEFAULT_PROMPT
    resolution = args[1] if len(args) > 1 else DEFAULT_RESOLUTION
    return asyncio.run(run(prompt, resolution))


if __name__ == "__main__":
    raise SystemExit(main())
