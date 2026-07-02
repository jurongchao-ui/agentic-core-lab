from __future__ import annotations

import json

from agentic_core.memory import JsonMemoryStore, build_memory_store_from_env


def test_json_memory_store_persists_notes_todos_memories_and_events(tmp_path) -> None:
    path = tmp_path / "memory.json"
    memory = JsonMemoryStore(path)

    memory.add_note("学习 Typed State")
    memory.add_todo("补充 memory 持久化")
    memory.add_long_term_memory(
        memory_type="preference",
        text="用户偏好: 学习任务控制在 30 分钟以内",
        reason="长期学习偏好",
        scores={"future_relevance": 3},
    )
    memory.record_event(event_type="run_started", run_id="run_1", payload={"goal": "hello"})

    loaded = JsonMemoryStore(path)

    assert loaded.notes[0].text == "学习 Typed State"
    assert loaded.todos[0].text == "补充 memory 持久化"
    assert loaded.long_term_memories[0].text == "用户偏好: 学习任务控制在 30 分钟以内"
    assert loaded.events[0].event_type == "run_started"


def test_json_memory_store_continues_event_ids_after_load(tmp_path) -> None:
    path = tmp_path / "memory.json"
    memory = JsonMemoryStore(path)
    first = memory.record_event(event_type="run_started", run_id="run_1", payload={})

    loaded = JsonMemoryStore(path)
    second = loaded.record_event(event_type="run_completed", run_id="run_1", payload={})

    assert first.id == "event_1"
    assert second.id == "event_2"


def test_json_memory_store_writes_stable_schema(tmp_path) -> None:
    path = tmp_path / "memory.json"
    memory = JsonMemoryStore(path)
    memory.add_note("n")
    memory.add_long_term_memory("preference", "用户偏好: 每次 30 分钟", "test", {})

    data = json.loads(path.read_text(encoding="utf-8"))

    assert data["schemaVersion"] == 1
    assert set(data) == {"schemaVersion", "notes", "todos", "long_term_memories", "events"}
    assert data["notes"][0]["createdAt"]
    assert data["long_term_memories"][0]["status"] == "active"
    assert data["long_term_memories"][0]["accessCount"] == 0


def test_json_memory_store_persists_memory_lifecycle_fields(tmp_path) -> None:
    path = tmp_path / "memory.json"
    memory = JsonMemoryStore(path)
    record = memory.add_long_term_memory("preference", "用户偏好: 每次 30 分钟", "test", {})
    memory.touch_long_term_memory(record.id)
    memory.archive_long_term_memory(record.id, "过期")

    loaded = JsonMemoryStore(path)
    loaded_record = loaded.long_term_memories[0]

    assert loaded_record.status == "archived"
    assert loaded_record.access_count == 1
    assert loaded_record.last_accessed_at is not None
    assert loaded_record.archive_reason == "过期"
    assert loaded.snapshot().long_term_memories == []


def test_json_memory_store_loads_old_memory_without_lifecycle_fields(tmp_path) -> None:
    path = tmp_path / "memory.json"
    path.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "notes": [],
                "todos": [],
                "long_term_memories": [
                    {
                        "id": "memory_1",
                        "type": "preference",
                        "text": "用户偏好: 每次 30 分钟",
                        "reason": "old",
                        "scores": {},
                        "createdAt": "2026-07-02T00:00:00+00:00",
                    }
                ],
                "events": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    memory = JsonMemoryStore(path)

    assert memory.long_term_memories[0].status == "active"
    assert memory.snapshot().long_term_memories[0].text == "用户偏好: 每次 30 分钟"


def test_build_memory_store_from_env_defaults_to_memory_store(monkeypatch) -> None:
    monkeypatch.delenv("AGENTIC_MEMORY_STORE", raising=False)
    monkeypatch.delenv("AGENTIC_MEMORY_PATH", raising=False)

    memory = build_memory_store_from_env()

    assert not isinstance(memory, JsonMemoryStore)


def test_build_memory_store_from_env_can_enable_json_store(tmp_path, monkeypatch) -> None:
    path = tmp_path / "memory.json"
    monkeypatch.setenv("AGENTIC_MEMORY_STORE", "json")
    monkeypatch.setenv("AGENTIC_MEMORY_PATH", str(path))

    memory = build_memory_store_from_env()

    assert isinstance(memory, JsonMemoryStore)
    assert memory.path == path
