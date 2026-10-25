"""Хранилище: атомарность, фильтры, TTL и защита путей."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import pytest

from kandinsky_mcp.api import errors as e
from kandinsky_mcp.domain.store import FileStore, StoredFile, TaskRecord


@pytest.fixture
def store(workdir: Path) -> FileStore:
    return FileStore(workdir)


def make_record(task_id: str = "1", **kwargs) -> TaskRecord:
    base = {
        "task_id": task_id,
        "route": "k6-image-t2i",
        "kind": "image",
        "prompt": "закат",
        "params": {"query": "закат", "resolution": "1024x1024"},
    }
    base.update(kwargs)
    return TaskRecord(**base)


def test_save_and_load_roundtrip(store: FileStore) -> None:
    record = make_record()
    record.files = [StoredFile(rel_path="results/2026-02-14/1.png", mime="image/png", bytes=3, sha256="ab")]
    record.status = "saved"
    store.save_task(record)

    loaded = store.require_task("1")
    assert loaded.status == "saved"
    assert loaded.params["resolution"] == "1024x1024"
    assert loaded.files[0].rel_path == "results/2026-02-14/1.png"


def test_save_is_atomic_no_tmp_left(store: FileStore) -> None:
    store.save_task(make_record())
    store.save_task(make_record())
    leftovers = list(store.tasks_dir.glob("*.tmp"))
    assert leftovers == []


def test_load_missing_returns_none_and_require_raises(store: FileStore) -> None:
    assert store.load_task("nope") is None
    with pytest.raises(e.KandinskyError) as info:
        store.require_task("nope")
    assert info.value.code == e.NOT_FOUND


def test_corrupted_json_is_treated_as_missing(store: FileStore) -> None:
    (store.tasks_dir / "broken.json").write_text("{не json", encoding="utf-8")
    assert store.load_task("broken") is None


def test_unsafe_task_id_is_rejected(store: FileStore) -> None:
    """task_id приходит из API, но путь из него строим мы — проверка обязательна."""
    assert store.load_task("../../etc/passwd") is None
    with pytest.raises(e.KandinskyError):
        store.save_task(make_record(task_id="../evil"))


def test_list_tasks_filters_and_orders(store: FileStore) -> None:
    store.save_task(make_record("1", status="saved", created_at=100.0))
    store.save_task(make_record("2", status="failed", created_at=200.0))
    store.save_task(make_record("3", route="k6_superres", status="saved", created_at=300.0))

    all_tasks = store.list_tasks(limit=10)
    assert [t.task_id for t in all_tasks] == ["3", "2", "1"]  # по убыванию времени

    assert [t.task_id for t in store.list_tasks(status="saved")] == ["3", "1"]
    assert [t.task_id for t in store.list_tasks(route="k6_superres")] == ["3"]
    assert len(store.list_tasks(limit=2)) == 2


def test_pending_tasks_excludes_terminal(store: FileStore) -> None:
    store.save_task(make_record("1", status="queued"))
    store.save_task(make_record("2", status="processing"))
    store.save_task(make_record("3", status="saved"))
    store.save_task(make_record("4", status="failed"))

    assert {t.task_id for t in store.pending_tasks()} == {"1", "2"}


def test_store_result_writes_file_with_hash(store: FileStore) -> None:
    record = make_record("42", created_at=time.mktime((2026, 2, 14, 12, 0, 0, 0, 0, 0)))
    payload = b"\x89PNG payload"

    stored = store.store_result(record, payload, "image/png", ".png")

    assert stored.sha256 == hashlib.sha256(payload).hexdigest()
    assert stored.bytes == len(payload)
    assert stored.rel_path.startswith("results/") and stored.rel_path.endswith("/42.png")
    assert "/" in stored.rel_path  # прямой слэш, независимо от ОС
    assert store.resolve(stored.rel_path).read_bytes() == payload


def test_resolve_rejects_traversal_and_absolute(store: FileStore) -> None:
    with pytest.raises(e.KandinskyError):
        store.resolve("../outside.txt")
    with pytest.raises(e.KandinskyError):
        store.resolve("results/../../outside.txt")
    # Абсолютный путь вне хранилища. Берём домашний каталог, а не «C:/…»:
    # на Linux строка «C:/Windows/win.ini» — относительный путь, и тест
    # проходил бы только на Windows (на этом и упал CI).
    with pytest.raises(e.KandinskyError):
        store.resolve(str(Path.home() / "outside.txt"))


def test_gc_removes_old_terminal_tasks_with_files(store: FileStore) -> None:
    old = make_record("old", status="saved", created_at=time.time() - 10 * 24 * 3600)
    stored = store.store_result(old, b"data", "image/png", ".png")
    old.files = [stored]
    store.save_task(old)

    fresh = make_record("fresh", status="saved", created_at=time.time())
    store.save_task(fresh)

    removed = store.gc(ttl_hours=24)

    assert removed == 1
    assert store.load_task("old") is None
    assert not store.resolve(stored.rel_path).exists()
    assert store.load_task("fresh") is not None


def test_gc_keeps_unfinished_tasks_even_if_old(store: FileStore) -> None:
    stuck = make_record("stuck", status="processing", created_at=time.time() - 10 * 24 * 3600)
    store.save_task(stuck)

    assert store.gc(ttl_hours=1) == 0
    assert store.load_task("stuck") is not None


def test_task_json_is_readable(store: FileStore) -> None:
    store.save_task(make_record())
    payload = json.loads((store.tasks_dir / "1.json").read_text(encoding="utf-8"))
    assert payload["route"] == "k6-image-t2i"
    assert payload["prompt"] == "закат"  # не ASCII-экранировано
