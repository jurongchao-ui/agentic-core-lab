from __future__ import annotations

from agentic_core.agent import Agent
from agentic_core.memory import JsonMemoryStore, MemoryStore
from agentic_core.memory_policy import RuleBasedMemoryPolicy
from agentic_core.planner import RuleBasedPlanner
from agentic_core.tools import ToolRegistry


def test_memory_store_deduplicates_exact_long_term_memories() -> None:
    memory = MemoryStore()

    first = memory.add_long_term_memory("preference", "用户偏好: 每次 30 分钟", "first", {})
    second = memory.add_long_term_memory("preference", " 用户偏好: 每次 30 分钟 ", "second", {})

    assert first == second
    assert len(memory.long_term_memories) == 1
    assert memory.long_term_memories[0].reason == "first"
    assert memory.long_term_memories[0].updated_at is not None


def test_json_memory_store_keeps_deduplicated_memory_after_reload(tmp_path) -> None:
    path = tmp_path / "memory.json"
    memory = JsonMemoryStore(path)

    memory.add_long_term_memory("preference", "用户偏好: 每次 30 分钟", "first", {})
    memory.add_long_term_memory("preference", "用户偏好: 每次 30 分钟", "second", {})

    loaded = JsonMemoryStore(path)

    assert len(loaded.long_term_memories) == 1
    assert loaded.long_term_memories[0].id == "memory_1"


def test_agent_repeated_preference_does_not_duplicate_long_term_memory() -> None:
    memory = MemoryStore()
    policy = RuleBasedMemoryPolicy()
    agent = Agent(
        planner=RuleBasedPlanner(),
        tools=ToolRegistry(memory, policy),
        memory=memory,
        memory_policy=policy,
    )

    agent.run("以后安排学习任务时，每次控制在30分钟以内")
    agent.run("以后安排学习任务时，每次控制在30分钟以内")

    assert len(memory.long_term_memories) == 1


def test_archive_long_term_memory_excludes_it_from_snapshot() -> None:
    memory = MemoryStore()
    record = memory.add_long_term_memory("preference", "用户偏好: 每次 30 分钟", "test", {})

    archived = memory.archive_long_term_memory(record.id, "用户偏好已过期")

    assert archived.status == "archived"
    assert archived.archived_at is not None
    assert archived.archive_reason == "用户偏好已过期"
    assert memory.long_term_memories == [archived]
    assert memory.snapshot().long_term_memories == []


def test_touch_long_term_memory_records_access() -> None:
    memory = MemoryStore()
    record = memory.add_long_term_memory("preference", "用户偏好: 每次 30 分钟", "test", {})

    touched = memory.touch_long_term_memory(record.id)

    assert touched.access_count == 1
    assert touched.last_accessed_at is not None
    assert touched.updated_at == touched.last_accessed_at


def test_archived_memory_does_not_block_new_active_memory_with_same_text() -> None:
    memory = MemoryStore()
    record = memory.add_long_term_memory("preference", "用户偏好: 每次 30 分钟", "old", {})
    memory.archive_long_term_memory(record.id, "old")

    new_record = memory.add_long_term_memory("preference", "用户偏好: 每次 30 分钟", "new", {})

    assert len(memory.long_term_memories) == 2
    assert new_record.id == "memory_2"
    assert memory.snapshot().long_term_memories == [new_record]


def test_snapshot_can_touch_only_active_long_term_memories() -> None:
    memory = MemoryStore()
    active = memory.add_long_term_memory("preference", "用户偏好: 每次 30 分钟", "active", {})
    archived = memory.add_long_term_memory("preference", "用户偏好: 每次 45 分钟", "archived", {})
    memory.archive_long_term_memory(archived.id, "old")

    snapshot = memory.snapshot(touch_long_term=True)

    assert snapshot.long_term_memories == [active]
    assert active.access_count == 1
    assert archived.access_count == 0


def test_agent_touches_memory_when_planner_receives_snapshot() -> None:
    memory = MemoryStore()
    policy = RuleBasedMemoryPolicy()
    record = memory.add_long_term_memory("preference", "用户偏好: 每次 30 分钟", "test", {})
    agent = Agent(
        planner=RuleBasedPlanner(),
        tools=ToolRegistry(memory, policy),
        memory=memory,
        memory_policy=policy,
    )

    agent.run("帮我安排 agentic memory 的学习计划")

    assert record.access_count >= 1
    assert record.last_accessed_at is not None
