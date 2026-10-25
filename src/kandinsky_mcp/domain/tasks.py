"""Менеджер задач: фоновый поллинг и однократное скачивание результата.

Зачем это отдельным слоем: Kandinsky API отдаёт результат ровно один раз и
хранит его 10 минут после готовности. Если переложить поллинг на агента, любая
задержка или повторный вызов инструмента превращаются в потерянную генерацию.
Поэтому задачу сопровождает собственный фоновый поллер, который ловит `done`
и сразу забирает файл на диск. Агент в любой момент видит локальное состояние.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from ..api.client import KandinskyClient
from ..api.errors import (
    NOT_FOUND,
    RESULT_GONE,
    UPSTREAM_ERROR,
    KandinskyError,
)
from ..api.routes import RouteRegistry
from .store import FileStore, TaskRecord, task_signature

log = logging.getLogger("kandinsky_mcp.tasks")

# Сколько всего ждём задачу, прежде чем сдаться. API роняет задачу на 45 минутах,
# берём с запасом, чтобы успеть зафиксировать терминальное состояние.
MAX_TASK_AGE_SECONDS = 50 * 60

# Сколько раз пробуем скачать результат: выдача одноразовая, но сеть может моргнуть.
DOWNLOAD_ATTEMPTS = 3


@dataclass(slots=True)
class _Counters:
    created: int = 0
    saved: int = 0
    failed: int = 0
    refused: int = 0
    deduped: int = 0  # сколько раз повтор запроса не потратил квоту


class TaskManager:
    def __init__(
        self,
        client: KandinskyClient,
        store: FileStore,
        registry: RouteRegistry,
        *,
        poll_min: float = 2.0,
        poll_max: float = 10.0,
        concurrency: int = 8,
        dedupe_window: float = 90.0,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._client = client
        self._store = store
        self._dedupe_window = dedupe_window
        self._registry = registry
        self._poll_min = poll_min
        self._poll_max = poll_max
        self._concurrency = asyncio.Semaphore(max(1, concurrency))
        self._sleep = sleep or asyncio.sleep
        self._pollers: dict[str, asyncio.Task[None]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self.counters = _Counters()
        self._closed = False

    # --- публичный API ----------------------------------------------------

    async def create(
        self,
        route: str,
        params: dict[str, Any],
        *,
        censor: bool | None,
        prompt: str,
        force_new: bool = False,
    ) -> TaskRecord:
        """Создаёт задачу и возвращает запись (без флага дедупликации)."""
        record, _ = await self.create_ex(
            route, params, censor=censor, prompt=prompt, force_new=force_new
        )
        return record

    async def create_ex(
        self,
        route: str,
        params: dict[str, Any],
        *,
        censor: bool | None,
        prompt: str,
        force_new: bool = False,
    ) -> tuple[TaskRecord, bool]:
        """Создаёт задачу в API и ставит её на фоновое сопровождение.

        Возвращает (запись, признак_повтора). Защита от повторов: если такой же
        запрос уже создавался в пределах `dedupe_window`, возвращаем ту задачу,
        не трогая квоту. Это лечит реальный случай — MCP-клиент не дождался
        ответа инструмента и отправил тот же вызов повторно, породив вторую
        платную генерацию.
        """
        spec = self._registry.get(route)

        if not force_new:
            existing = self._store.find_duplicate(
                route=route,
                params=params,
                prompt=prompt,
                window_seconds=self._dedupe_window,
            )
            if existing is not None:
                self.counters.deduped += 1
                log.info(
                    "повтор запроса: возвращаю задачу %s вместо новой (экономия квоты)",
                    existing.task_id,
                )
                return existing, True

        task_id = await self._client.create_task(route, params, censor=censor)

        record = TaskRecord(
            task_id=task_id,
            route=route,
            kind=spec.kind,
            prompt=prompt,
            params=params,
            censor=censor,
            status="queued",
            signature=task_signature(params, prompt),
        )
        self._store.save_task(record)
        self.counters.created += 1
        self._spawn(task_id)
        return record, False

    async def refresh(self, task_id: str) -> TaskRecord:
        """Однократная проверка статуса по запросу агента.

        Если задача уже готова — результат скачивается здесь же: ждать
        следующего тика поллера нельзя, окно выдачи всего 10 минут.
        """
        record = self._store.require_task(task_id)
        if record.is_terminal:
            return record

        try:
            status = await self._client.get_status(task_id)
        except KandinskyError as exc:
            if exc.code in {NOT_FOUND, UPSTREAM_ERROR} and not exc.retryable:
                raise
            return record

        return await self._apply_status(record, status.status, status.error)

    async def get(self, task_id: str) -> TaskRecord:
        return self._store.require_task(task_id)

    def list_active(self) -> list[str]:
        return [tid for tid, task in self._pollers.items() if not task.done()]

    async def resume(self) -> int:
        """После рестарта поднимает поллеры для незавершённых задач."""
        pending = self._store.pending_tasks()
        for record in pending:
            self._spawn(record.task_id)
        return len(pending)

    async def close(self) -> None:
        self._closed = True
        for task in list(self._pollers.values()):
            task.cancel()
        for task in list(self._pollers.values()):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    # --- внутреннее -------------------------------------------------------

    def _spawn(self, task_id: str) -> None:
        if self._closed:
            return
        existing = self._pollers.get(task_id)
        if existing is not None and not existing.done():
            return
        self._pollers[task_id] = asyncio.create_task(
            self._poll_loop(task_id), name=f"kandinsky-poll-{task_id}"
        )

    def _lock(self, task_id: str) -> asyncio.Lock:
        lock = self._locks.get(task_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[task_id] = lock
        return lock

    async def _poll_loop(self, task_id: str) -> None:
        """Опрашивает API, пока задача не станет терминальной."""
        deadline = time.monotonic() + MAX_TASK_AGE_SECONDS
        delay = self._poll_min
        consecutive_errors = 0

        async with self._concurrency:
            while not self._closed:
                record = self._store.load_task(task_id)
                if record is None or record.is_terminal:
                    return
                if time.monotonic() > deadline:
                    await self._fail(record, "Локальный таймаут ожидания результата (50 минут).")
                    return

                await self._sleep(delay)
                delay = min(delay * 1.5, self._poll_max)

                try:
                    status = await self._client.get_status(task_id)
                except KandinskyError as exc:
                    if exc.retryable:
                        consecutive_errors += 1
                        if consecutive_errors >= 10:
                            await self._fail(record, f"API недоступен: {exc.message}")
                            return
                        continue
                    await self._fail(record, exc.to_agent_text())
                    return

                consecutive_errors = 0
                record.polls += 1
                updated = await self._apply_status(record, status.status, status.error)
                if updated.is_terminal:
                    return

    async def _apply_status(
        self, record: TaskRecord, status: str, error: str | None
    ) -> TaskRecord:
        """Переводит состояние задачи и при `done` сразу забирает файл."""
        if status == "done":
            async with self._lock(record.task_id):
                fresh = self._store.load_task(record.task_id) or record
                if fresh.is_terminal:
                    return fresh
                return await self._download(fresh)
        if status == "failed":
            await self._fail(record, error or "Задача завершилась ошибкой на стороне API.")
            return self._store.load_task(record.task_id) or record

        if record.status != status:
            record.status = status  # type: ignore[assignment]
        # Сохраняем на каждом опросе, а не только при смене статуса: иначе
        # счётчик polls и updated_at остаются в памяти и теряются при рестарте.
        self._store.save_task(record)
        return record

    async def _download(self, record: TaskRecord) -> TaskRecord:
        """Скачивает результат ровно один раз и сохраняет на диск."""
        last_error: KandinskyError | None = None
        for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
            try:
                media = await self._client.download_result(record.task_id)
            except KandinskyError as exc:
                last_error = exc
                if exc.code == RESULT_GONE:
                    await self._fail(record, exc.to_agent_text())
                    return self._store.load_task(record.task_id) or record
                if not exc.retryable:
                    await self._fail(record, exc.to_agent_text())
                    return self._store.load_task(record.task_id) or record
                await self._sleep(min(1.0 * attempt, 5.0))
                continue

            stored = self._store.store_result(record, media.content, media.content_type, media.extension)
            record.files = [stored]
            record.status = "saved"
            record.error = None
            record.finished_at = time.time()
            self._store.save_task(record)
            self.counters.saved += 1
            return record

        message = last_error.to_agent_text() if last_error else "Не удалось скачать результат."
        await self._fail(record, message)
        return self._store.load_task(record.task_id) or record

    async def _fail(self, record: TaskRecord, message: str) -> None:
        record.status = "failed"
        record.error = message
        record.finished_at = time.time()
        self._store.save_task(record)
        self.counters.failed += 1
