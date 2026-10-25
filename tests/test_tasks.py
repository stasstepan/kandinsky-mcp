"""Фоновый поллер: результат не теряется, состояние согласовано.

Ключевая проверка архитектуры — «одноразовая выдача» API не должна приводить
к потерянной генерации, даже если агент вообще не поллит задачу.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest

from kandinsky_mcp.api.client import DownloadedFile, TaskStatus
from kandinsky_mcp.api.errors import KandinskyError
from kandinsky_mcp.api.routes import RouteRegistry
from kandinsky_mcp.domain.store import FileStore, TaskRecord
from kandinsky_mcp.domain.tasks import TaskManager

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 40


class FakeClient:
    """Скриптованный клиент API: статусы по шагам, скачивание однократное."""

    def __init__(
        self,
        statuses: list[str],
        *,
        error: str | None = None,
        download_error: KandinskyError | None = None,
    ) -> None:
        self._statuses = list(statuses)
        self._error = error
        self._download_error = download_error
        self.downloads = 0
        self.created: list[tuple[str, dict[str, Any], bool | None]] = []

    async def create_task(self, route: str, params: dict[str, Any], *, censor: bool | None = None) -> str:
        self.created.append((route, params, censor))
        return "777"

    async def get_status(self, task_id: str) -> TaskStatus:
        status = self._statuses.pop(0) if len(self._statuses) > 1 else self._statuses[0]
        return TaskStatus(status=status, error=self._error if status == "failed" else None)

    async def download_result(self, task_id: str) -> DownloadedFile:
        self.downloads += 1
        if self._download_error is not None:
            raise self._download_error
        return DownloadedFile(content=PNG, content_type="image/png")


@pytest.fixture
def store(workdir: Path) -> FileStore:
    return FileStore(workdir)


def make_manager(client: FakeClient, store: FileStore, *, dedupe_window: float = 90.0) -> TaskManager:
    return TaskManager(
        client,  # type: ignore[arg-type]
        store,
        RouteRegistry(),
        poll_min=0.01,
        poll_max=0.02,
        dedupe_window=dedupe_window,
        sleep=asyncio.sleep,
    )


async def wait_terminal(store: FileStore, task_id: str, timeout: float = 5.0) -> TaskRecord:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = store.load_task(task_id)
        if record is not None and record.is_terminal:
            return record
        await asyncio.sleep(0.01)
    raise AssertionError(f"задача {task_id} не завершилась за {timeout} с")


async def test_result_is_downloaded_without_agent_polling(store: FileStore) -> None:
    """Агент вообще не поллит — файл всё равно оказывается на диске."""
    client = FakeClient(["queued", "processing", "processing", "done"])
    manager = make_manager(client, store)

    await manager.create("k6-image-t2i", {"query": "кот"}, censor=True, prompt="кот")
    record = await wait_terminal(store, "777")

    assert record.status == "saved"
    assert client.downloads == 1, "результат скачивается ровно один раз"
    assert len(record.files) == 1
    assert store.resolve(record.files[0].rel_path).read_bytes() == PNG
    await manager.close()


async def test_failed_task_keeps_error_and_does_not_download(store: FileStore) -> None:
    client = FakeClient(["queued", "failed"], error="Task failed due to generation timeout")
    manager = make_manager(client, store)

    await manager.create("k6-image-t2i", {"query": "кот"}, censor=True, prompt="кот")
    record = await wait_terminal(store, "777")

    assert record.status == "failed"
    assert "generation timeout" in (record.error or "")
    assert client.downloads == 0
    await manager.close()


async def test_result_gone_is_reported_not_retried_forever(store: FileStore) -> None:
    client = FakeClient(
        ["done"],
        download_error=KandinskyError("result_gone", "already delivered", 422),
    )
    manager = make_manager(client, store)

    await manager.create("k6-image-t2i", {"query": "кот"}, censor=True, prompt="кот")
    record = await wait_terminal(store, "777")

    assert record.status == "failed"
    assert "result_gone" in (record.error or "")
    assert client.downloads == 1
    await manager.close()


async def test_retryable_download_error_is_retried(store: FileStore) -> None:
    class FlakyClient(FakeClient):
        def __init__(self) -> None:
            super().__init__(["done"])
            self.calls = 0

        async def download_result(self, task_id: str) -> DownloadedFile:
            self.calls += 1
            if self.calls < 3:
                raise KandinskyError("upstream_error", "502", 502)
            return DownloadedFile(content=PNG, content_type="image/png")

    client = FlakyClient()
    manager = make_manager(client, store)

    await manager.create("k6-image-t2i", {"query": "кот"}, censor=True, prompt="кот")
    record = await wait_terminal(store, "777")

    assert record.status == "saved"
    assert client.calls == 3
    await manager.close()


async def test_refresh_downloads_immediately_when_done(store: FileStore) -> None:
    """Ручная проверка агентом тоже не должна терять результат."""
    client = FakeClient(["done"])
    manager = make_manager(client, store)

    record = TaskRecord(task_id="900", route="k6-image-t2i", kind="image", prompt="кот", status="processing")
    store.save_task(record)

    refreshed = await manager.refresh("900")

    assert refreshed.status == "saved"
    assert client.downloads == 1
    await manager.close()


async def test_concurrent_refreshes_download_only_once(store: FileStore) -> None:
    """Поллер и ручной refresh не должны скачивать один файл дважды."""
    client = FakeClient(["done"])
    manager = make_manager(client, store)

    store.save_task(
        TaskRecord(task_id="901", route="k6-image-t2i", kind="image", prompt="кот", status="processing")
    )

    await asyncio.gather(manager.refresh("901"), manager.refresh("901"), manager.refresh("901"))

    assert client.downloads == 1
    assert store.require_task("901").status == "saved"
    await manager.close()


async def test_resume_picks_up_unfinished_tasks_after_restart(store: FileStore) -> None:
    store.save_task(
        TaskRecord(task_id="500", route="k6-image-t2i", kind="image", prompt="кот", status="processing")
    )
    client = FakeClient(["done"])
    manager = make_manager(client, store)

    resumed = await manager.resume()
    record = await wait_terminal(store, "500")

    assert resumed == 1
    assert record.status == "saved"
    await manager.close()


async def test_resume_ignores_finished_tasks(store: FileStore) -> None:
    store.save_task(
        TaskRecord(task_id="501", route="k6-image-t2i", kind="image", prompt="кот", status="saved")
    )
    manager = make_manager(FakeClient(["done"]), store)

    assert await manager.resume() == 0
    await manager.close()


async def test_create_passes_route_params_and_censor(store: FileStore) -> None:
    client = FakeClient(["done"])
    manager = make_manager(client, store)

    await manager.create(
        "k6-image-t2i",
        {"query": "кот", "resolution": "1024x1024"},
        censor=False,
        prompt="кот",
    )
    await wait_terminal(store, "777")

    assert client.created == [("k6-image-t2i", {"query": "кот", "resolution": "1024x1024"}, False)]
    await manager.close()


async def test_close_cancels_pollers(store: FileStore) -> None:
    client = FakeClient(["processing"])  # никогда не завершится
    manager = make_manager(client, store)

    await manager.create("k6-image-t2i", {"query": "кот"}, censor=True, prompt="кот")
    await asyncio.sleep(0.05)
    assert manager.list_active()

    await manager.close()
    assert manager.list_active() == []


async def test_unknown_task_raises_not_found(store: FileStore) -> None:
    manager = make_manager(FakeClient(["done"]), store)
    with pytest.raises(KandinskyError):
        await manager.get("does-not-exist")
    await manager.close()


# --- защита от повторов (двойной расход квоты) -------------------------------


class CountingClient(FakeClient):
    """Выдаёт уникальные task_id, чтобы повторы были различимы."""

    def __init__(self, statuses: list[str] | None = None) -> None:
        super().__init__(statuses or ["done"])
        self._n = 0

    async def create_task(self, route: str, params: dict[str, Any], *, censor: bool | None = None) -> str:
        self._n += 1
        self.created.append((route, params, censor))
        return f"task-{self._n}"


async def test_identical_request_within_window_reuses_task(store: FileStore) -> None:
    """Повтор того же вызова не должен создавать вторую платную генерацию."""
    client = CountingClient()
    manager = make_manager(client, store)

    first = await manager.create("k6-image-t2i", {"query": "кот"}, censor=True, prompt="кот")
    second = await manager.create("k6-image-t2i", {"query": "кот"}, censor=True, prompt="кот")

    assert second.task_id == first.task_id
    assert len(client.created) == 1, "в API должен уйти ровно один запрос"
    assert manager.counters.created == 1
    assert manager.counters.deduped == 1
    await manager.close()


async def test_force_new_creates_second_task(store: FileStore) -> None:
    client = CountingClient()
    manager = make_manager(client, store)

    first = await manager.create("k6-image-t2i", {"query": "кот"}, censor=True, prompt="кот")
    second = await manager.create(
        "k6-image-t2i", {"query": "кот"}, censor=True, prompt="кот", force_new=True
    )

    assert second.task_id != first.task_id
    assert len(client.created) == 2
    await manager.close()


async def test_different_prompt_is_not_deduplicated(store: FileStore) -> None:
    client = CountingClient()
    manager = make_manager(client, store)

    first = await manager.create("k6-image-t2i", {"query": "кот"}, censor=True, prompt="кот")
    second = await manager.create("k6-image-t2i", {"query": "пёс"}, censor=True, prompt="пёс")

    assert second.task_id != first.task_id
    assert len(client.created) == 2
    await manager.close()


async def test_different_resolution_is_not_deduplicated(store: FileStore) -> None:
    client = CountingClient()
    manager = make_manager(client, store)

    await manager.create(
        "k6-image-t2i", {"query": "кот", "resolution": "1024x1024"}, censor=True, prompt="кот"
    )
    await manager.create(
        "k6-image-t2i", {"query": "кот", "resolution": "768x768"}, censor=True, prompt="кот"
    )

    assert len(client.created) == 2
    await manager.close()


async def test_failed_task_is_not_reused(store: FileStore) -> None:
    """Провал повторяем честно: кэшировать ошибку как результат нельзя."""
    client = CountingClient(["failed"])
    manager = make_manager(client, store)

    first = await manager.create("k6-image-t2i", {"query": "кот"}, censor=True, prompt="кот")
    record = await wait_terminal(store, first.task_id)
    assert record.status == "failed"

    second = await manager.create("k6-image-t2i", {"query": "кот"}, censor=True, prompt="кот")
    assert second.task_id != first.task_id
    assert len(client.created) == 2
    await manager.close()


async def test_dedupe_window_zero_disables_protection(store: FileStore) -> None:
    client = CountingClient()
    manager = make_manager(client, store, dedupe_window=0)

    await manager.create("k6-image-t2i", {"query": "кот"}, censor=True, prompt="кот")
    await manager.create("k6-image-t2i", {"query": "кот"}, censor=True, prompt="кот")

    assert len(client.created) == 2
    await manager.close()
