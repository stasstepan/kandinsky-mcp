"""Хранение задач и результатов на диске.

Задача — JSON-файл, результат — бинарник. Никакой БД: состояние небольшое,
а JSON переживает рестарт контейнера и читается глазами при отладке.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from ..api.errors import NOT_FOUND, KandinskyError

TaskState = Literal["queued", "processing", "saved", "failed", "cancelled"]

TERMINAL_STATES: frozenset[str] = frozenset({"saved", "failed", "cancelled"})

_SAFE_TASK_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _now() -> float:
    return time.time()


def task_signature(params: dict[str, Any], prompt: str) -> str:
    """Отпечаток запроса для защиты от повторов.

    Считаем инкрементально: параметры могут содержать base64 картинок
    на десятки мегабайт, и склеивать их в одну строку незачем.
    """
    digest = hashlib.sha256()
    digest.update(prompt.encode("utf-8"))
    for key in sorted(params):
        digest.update(b"\x00")
        digest.update(key.encode("utf-8"))
        value = params[key]
        if isinstance(value, list):
            for item in value:
                digest.update(b"\x01")
                digest.update(str(item).encode("utf-8"))
        else:
            digest.update(b"\x02")
            digest.update(str(value).encode("utf-8"))
    return digest.hexdigest()


@dataclass(slots=True)
class StoredFile:
    """Сохранённый результат. Путь относительный, чтобы /data можно было переносить."""

    rel_path: str
    mime: str
    bytes: int
    sha256: str

    @property
    def filename(self) -> str:
        return Path(self.rel_path).name


@dataclass(slots=True)
class TaskRecord:
    task_id: str
    route: str
    kind: str
    prompt: str
    params: dict[str, Any] = field(default_factory=dict)
    censor: bool | None = None
    status: TaskState = "queued"
    error: str | None = None
    created_at: float = field(default_factory=_now)
    updated_at: float = field(default_factory=_now)
    finished_at: float | None = None
    files: list[StoredFile] = field(default_factory=list)
    polls: int = 0
    # Отпечаток запроса (params + prompt): нужен для защиты от повторов.
    signature: str = ""

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATES

    @property
    def duration_ms(self) -> int | None:
        if self.finished_at is None:
            return None
        return int((self.finished_at - self.created_at) * 1000)

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["files"] = [asdict(f) for f in self.files]
        return data

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> TaskRecord:
        files = [StoredFile(**f) for f in data.pop("files", [])]
        record = cls(**data)
        record.files = files
        return record


class FileStore:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.tasks_dir = self.data_dir / "tasks"
        self.results_dir = self.data_dir / "results"
        for directory in (self.tasks_dir, self.results_dir):
            directory.mkdir(parents=True, exist_ok=True)

    # --- задачи -----------------------------------------------------------

    def _task_path(self, task_id: str) -> Path:
        if not _SAFE_TASK_ID.match(task_id):
            # task_id приходит из API, но пишем мы — проверяем, чтобы не собрать путь из мусора.
            raise KandinskyError(NOT_FOUND, f"Некорректный task_id: {task_id!r}")
        return self.tasks_dir / f"{task_id}.json"

    def save_task(self, record: TaskRecord) -> None:
        record.updated_at = _now()
        path = self._task_path(record.task_id)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(record.to_json(), ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)  # атомарная замена: читатель не увидит половину файла

    def load_task(self, task_id: str) -> TaskRecord | None:
        try:
            path = self._task_path(task_id)
        except KandinskyError:
            return None
        if not path.exists():
            return None
        try:
            return TaskRecord.from_json(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, TypeError, ValueError):
            return None

    def require_task(self, task_id: str) -> TaskRecord:
        record = self.load_task(task_id)
        if record is None:
            raise KandinskyError(
                NOT_FOUND,
                f"Задача {task_id!r} неизвестна этому серверу. "
                "Возможно, она создана другим инстансом или удалена по TTL.",
            )
        return record

    def list_tasks(
        self,
        *,
        limit: int = 20,
        status: str | None = None,
        route: str | None = None,
    ) -> list[TaskRecord]:
        records: list[TaskRecord] = []
        for path in self.tasks_dir.glob("*.json"):
            try:
                record = TaskRecord.from_json(json.loads(path.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, TypeError, ValueError, OSError):
                continue
            if status and record.status != status:
                continue
            if route and record.route != route:
                continue
            records.append(record)
        records.sort(key=lambda r: r.created_at, reverse=True)
        return records[: max(1, limit)]

    def pending_tasks(self) -> list[TaskRecord]:
        return [r for r in self.list_tasks(limit=10_000) if not r.is_terminal]

    def find_duplicate(
        self,
        *,
        route: str,
        params: dict[str, Any],
        prompt: str,
        window_seconds: float,
    ) -> TaskRecord | None:
        """Ищет задачу с теми же параметрами, которую можно переиспользовать.

        Два случая:
          1. повтор в пределах окна — клиент не дождался ответа и отправил
             тот же вызов ещё раз;
          2. задача с таким же запросом всё ещё выполняется (queued/processing) —
             возвращаем её независимо от возраста.

        Второй случай важен из-за лимита параллельных задач (у API их всего две):
        повтор, созданный через пару минут, занимал второй слот, и новые
        генерации начинали отбиваться с «2/2 active tasks».
        """
        signature = task_signature(params, prompt)
        cutoff = _now() - window_seconds if window_seconds > 0 else None

        for record in self.list_tasks(limit=200):
            if record.route != route or record.status == "failed":
                continue
            if not record.signature or record.signature != signature:
                continue
            still_running = not record.is_terminal
            if still_running or (cutoff is not None and record.created_at >= cutoff):
                return record
        return None

    # --- результаты -------------------------------------------------------

    def store_result(self, record: TaskRecord, content: bytes, mime: str, extension: str) -> StoredFile:
        """Кладёт бинарник в results/<дата>/<task_id><ext>."""
        day = time.strftime("%Y-%m-%d", time.gmtime(record.created_at))
        directory = self.results_dir / day
        directory.mkdir(parents=True, exist_ok=True)

        filename = f"{record.task_id}{extension}"
        path = directory / filename
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(content)
        os.replace(tmp, path)

        rel = str(path.relative_to(self.data_dir)).replace(os.sep, "/")
        return StoredFile(
            rel_path=rel,
            mime=mime,
            bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
        )

    def resolve(self, rel_path: str) -> Path:
        """Безопасно превращает относительный путь в абсолютный внутри data_dir.

        Отсекает `..`, абсолютные пути и переходы по симлинкам наружу.
        """
        candidate = (self.data_dir / rel_path).resolve()
        root = self.data_dir.resolve()
        if not str(candidate).startswith(str(root) + os.sep) and candidate != root:
            raise KandinskyError(NOT_FOUND, "Путь вне каталога данных")
        return candidate

    # --- уборка -----------------------------------------------------------

    def gc(self, ttl_hours: int) -> int:
        """Удаляет задачи и файлы старше TTL. Возвращает число удалённых задач."""
        cutoff = _now() - ttl_hours * 3600
        removed = 0
        for record in self.list_tasks(limit=10_000):
            if record.created_at >= cutoff or not record.is_terminal:
                continue
            for stored in record.files:
                with contextlib.suppress(KandinskyError, OSError):
                    self.resolve(stored.rel_path).unlink(missing_ok=True)
            with contextlib.suppress(KandinskyError, OSError):
                self._task_path(record.task_id).unlink(missing_ok=True)
            removed += 1
        return removed
