from __future__ import annotations

from datetime import datetime, timezone

from agentic_core.memory.store import MemoryStore
from agentic_core.memory.lifecycle import MemoryLifecyclePolicy
from agentic_core.runtime.schemas import MemoryRecord


def test_lifecycle_policy_separates_semantic_key_from_conflict_key() -> None:
    policy = MemoryLifecyclePolicy()
    memory = MemoryRecord(
        id="memory_1",
        memory_type="preference",
        text="用户偏好: 每次 30 分钟",
        reason="test",
        scores={},
        created_at="2026-07-04T00:00:00+00:00",
    )

    assert policy.semantic_key(memory.memory_type, memory.text) is None
    assert policy.conflict_key(memory) == "preference:study_session_duration"


def test_lifecycle_policy_generates_shared_tech_stack_key() -> None:
    policy = MemoryLifecyclePolicy()
    memory = MemoryRecord(
        id="memory_1",
        memory_type="user_profile",
        text="用户技术栈: Node.js、React、Codex",
        reason="test",
        scores={},
        created_at="2026-07-04T00:00:00+00:00",
    )

    assert policy.semantic_key(memory.memory_type, memory.text) == "user_profile:tech_stack"
    assert policy.conflict_key(memory) == "user_profile:tech_stack"


def test_lifecycle_policy_controls_importance_expiry_and_retention_key() -> None:
    policy = MemoryLifecyclePolicy(task_memory_ttl_days=7)
    memory = MemoryRecord(
        id="memory_1",
        memory_type="task_state",
        text="任务状态: 正在实现 Memory Lifecycle",
        reason="test",
        scores={"task_continuity": 5},
        created_at="2026-07-04T00:00:00+00:00",
        updated_at="2026-07-04T00:00:00+00:00",
        importance=policy.memory_importance("task_state", {"task_continuity": 5}),
        access_count=2,
    )
    memory.expires_at = policy.default_expiry(memory.memory_type, memory.created_at)

    assert memory.importance > 0
    assert memory.expires_at == "2026-07-11T00:00:00+00:00"
    assert not policy.is_expired(memory, datetime(2026, 7, 10, tzinfo=timezone.utc))
    assert policy.is_expired(memory, datetime(2026, 7, 12, tzinfo=timezone.utc))
    assert policy.retention_sort_key(memory) == (memory.importance, 2, 1783123200.0)


def test_memory_store_uses_injected_lifecycle_policy_for_task_expiry() -> None:
    memory = MemoryStore(lifecycle_policy=MemoryLifecyclePolicy(task_memory_ttl_days=3))

    record = memory.add_long_term_memory(
        "task_state",
        "任务状态: 自定义过期策略",
        "test",
        {"task_continuity": 5},
    )

    assert record.expires_at is not None
    assert record.expires_at.startswith("20")
    assert record.expires_at[:10] > record.created_at[:10]
