from __future__ import annotations

import json

import pytest

from agentic_core.memory.store import JsonMemoryStore
from agentic_core.memory.admin import (
    archive_memory,
    find_memory_conflicts,
    list_memories,
    main,
    resolve_memory_conflict,
    set_memory_importance,
)
from agentic_core.runtime.schemas import MemoryRecord


def test_memory_admin_lists_namespace_memories(tmp_path, capsys) -> None:
    path = seed_memory_store(tmp_path)

    code = main(
        [
            "list",
            "--path",
            str(path),
            "--user-id",
            "user_a",
            "--tenant-id",
            "tenant_a",
            "--json",
        ]
    )
    data = json.loads(capsys.readouterr().out)

    assert code == 0
    assert data["type"] == "agentic_memory_review_list"
    assert data["count"] == 1
    assert data["memories"][0]["text"] == "用户偏好: 每次 30 分钟"
    assert data["memories"][0]["userId"] == "user_a"
    assert data["memories"][0]["tenantId"] == "tenant_a"


def test_memory_admin_can_include_archived_and_filter_type(tmp_path) -> None:
    path = seed_memory_store(tmp_path)
    store = JsonMemoryStore(path)
    archived = store.add_long_term_memory(
        "long_term_note",
        "长期笔记: 旧想法",
        "old",
        {},
        user_id="user_a",
        tenant_id="tenant_a",
    )
    store.archive_long_term_memory(archived.id, "reviewed")

    active_only = list_memories(store, "user_a", "tenant_a")
    notes = list_memories(store, "user_a", "tenant_a", include_archived=True, memory_type="long_term_note")

    assert [memory.id for memory in active_only] == ["memory_1"]
    assert notes == [archived]


def test_memory_admin_archives_memory_in_namespace_and_persists(tmp_path, capsys) -> None:
    path = seed_memory_store(tmp_path)

    code = main(
        [
            "archive",
            "--path",
            str(path),
            "--user-id",
            "user_a",
            "--tenant-id",
            "tenant_a",
            "--memory-id",
            "memory_1",
            "--reason",
            "人工审核归档",
            "--json",
        ]
    )
    data = json.loads(capsys.readouterr().out)
    loaded = JsonMemoryStore(path)

    assert code == 0
    assert data["type"] == "agentic_memory_review_archive"
    assert data["memory"]["status"] == "archived"
    assert loaded.long_term_memories[0].status == "archived"
    assert loaded.long_term_memories[0].archive_reason == "人工审核归档"


def test_memory_admin_sets_importance_in_namespace_and_persists(tmp_path, capsys) -> None:
    path = seed_memory_store(tmp_path)

    code = main(
        [
            "set-importance",
            "--path",
            str(path),
            "--user-id",
            "user_a",
            "--tenant-id",
            "tenant_a",
            "--memory-id",
            "memory_1",
            "--importance",
            "99",
            "--json",
        ]
    )
    data = json.loads(capsys.readouterr().out)
    loaded = JsonMemoryStore(path)

    assert code == 0
    assert data["type"] == "agentic_memory_review_importance"
    assert data["memory"]["importance"] == 99
    assert loaded.long_term_memories[0].importance == 99


def test_memory_admin_rejects_cross_namespace_updates(tmp_path) -> None:
    path = seed_memory_store(tmp_path)
    store = JsonMemoryStore(path)

    with pytest.raises(ValueError, match="namespace"):
        archive_memory(store, "memory_1", "wrong user", user_id="user_b", tenant_id="tenant_a")

    with pytest.raises(ValueError, match="namespace"):
        set_memory_importance(store, "memory_1", 50, user_id="user_a", tenant_id="tenant_b")


def test_memory_admin_detects_conflicts_in_namespace(tmp_path, capsys) -> None:
    path = seed_conflicting_memory_store(tmp_path)

    code = main(
        [
            "conflicts",
            "--path",
            str(path),
            "--user-id",
            "user_a",
            "--tenant-id",
            "tenant_a",
            "--json",
        ]
    )
    data = json.loads(capsys.readouterr().out)

    assert code == 0
    assert data["type"] == "agentic_memory_review_conflicts"
    assert data["count"] == 1
    assert data["conflicts"][0]["key"] == "preference:study_session_duration"
    assert data["conflicts"][0]["count"] == 2
    assert [item["id"] for item in data["conflicts"][0]["memories"]] == ["memory_1", "memory_3"]


def test_memory_admin_resolves_conflict_by_archiving_other_memories(tmp_path, capsys) -> None:
    path = seed_conflicting_memory_store(tmp_path)

    code = main(
        [
            "resolve-conflict",
            "--path",
            str(path),
            "--user-id",
            "user_a",
            "--tenant-id",
            "tenant_a",
            "--keep-memory-id",
            "memory_3",
            "--reason",
            "保留最新学习时长偏好",
            "--json",
        ]
    )
    data = json.loads(capsys.readouterr().out)
    loaded = JsonMemoryStore(path)
    active_ids = [memory.id for memory in list_memories(loaded, "user_a", "tenant_a")]

    assert code == 0
    assert data["type"] == "agentic_memory_review_resolve_conflict"
    assert data["keptMemory"]["id"] == "memory_3"
    assert data["archivedCount"] == 1
    assert data["archivedMemories"][0]["id"] == "memory_1"
    assert active_ids == ["memory_3"]
    assert loaded.long_term_memories[0].status == "archived"
    assert loaded.long_term_memories[0].archive_reason == "保留最新学习时长偏好"


def test_memory_admin_rejects_conflict_resolution_outside_namespace(tmp_path) -> None:
    path = seed_conflicting_memory_store(tmp_path)
    store = JsonMemoryStore(path)

    with pytest.raises(ValueError, match="namespace"):
        resolve_memory_conflict(
            store,
            user_id="user_b",
            tenant_id="tenant_a",
            keep_memory_id="memory_1",
            reason="wrong namespace",
        )


def test_memory_admin_rejects_conflict_resolution_without_conflict(tmp_path) -> None:
    path = seed_memory_store(tmp_path)
    store = JsonMemoryStore(path)

    with pytest.raises(ValueError, match="not part of an active conflict"):
        resolve_memory_conflict(
            store,
            user_id="user_a",
            tenant_id="tenant_a",
            keep_memory_id="memory_1",
            reason="no conflict",
        )


def seed_memory_store(tmp_path) -> str:
    path = tmp_path / "memory.json"
    store = JsonMemoryStore(path)
    store.add_long_term_memory(
        "preference",
        "用户偏好: 每次 30 分钟",
        "test",
        {},
        user_id="user_a",
        tenant_id="tenant_a",
    )
    store.add_long_term_memory(
        "preference",
        "用户偏好: 每次 45 分钟",
        "other user",
        {},
        user_id="user_b",
        tenant_id="tenant_a",
    )
    return str(path)


def seed_conflicting_memory_store(tmp_path) -> str:
    path = seed_memory_store(tmp_path)
    store = JsonMemoryStore(path)
    store.long_term_memories.append(
        MemoryRecord(
            id="memory_3",
            memory_type="preference",
            text="用户偏好: 学习任务每次控制在 45 分钟",
            reason="manual import",
            scores={},
            created_at="2026-07-04T00:00:00+00:00",
            updated_at="2026-07-04T00:00:00+00:00",
            user_id="user_a",
            tenant_id="tenant_a",
        )
    )
    store.long_term_memories.append(
        MemoryRecord(
            id="memory_4",
            memory_type="preference",
            text="用户偏好: 学习任务每次控制在 60 分钟",
            reason="other tenant",
            scores={},
            created_at="2026-07-04T00:00:00+00:00",
            updated_at="2026-07-04T00:00:00+00:00",
            user_id="user_a",
            tenant_id="tenant_b",
        )
    )
    store.save()
    conflicts = find_memory_conflicts(store, "user_a", "tenant_a")
    assert len(conflicts) == 1
    return str(path)
