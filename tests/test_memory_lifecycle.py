from __future__ import annotations

from agentic_core.runtime.agent import Agent
from agentic_core.memory.store import JsonMemoryStore, MemoryStore
from agentic_core.runtime.contracts import PlannerContext
from agentic_core.policies.memory import RuleBasedMemoryPolicy
from agentic_core.policies.planner import RuleBasedPlanner
from agentic_core.runtime.schemas import Action
from agentic_core.runtime.context import RuntimeIdentity
from agentic_core.tools.registry import ToolRegistry


def test_memory_store_deduplicates_exact_long_term_memories() -> None:
    memory = MemoryStore()

    first = memory.add_long_term_memory("preference", "用户偏好: 每次 30 分钟", "first", {})
    second = memory.add_long_term_memory("preference", " 用户偏好: 每次 30 分钟 ", "second", {})

    assert first == second
    assert len(memory.long_term_memories) == 1
    assert memory.long_term_memories[0].reason == "first"
    assert memory.long_term_memories[0].updated_at is not None
    assert memory.long_term_memories[0].importance >= 0


def test_long_term_memories_are_namespaced_by_user_and_tenant() -> None:
    memory = MemoryStore()

    user_a = memory.add_long_term_memory(
        "preference",
        "用户偏好: 每次 30 分钟",
        "a",
        {},
        user_id="user_a",
        tenant_id="tenant_a",
    )
    user_b = memory.add_long_term_memory(
        "preference",
        "用户偏好: 每次 30 分钟",
        "b",
        {},
        user_id="user_b",
        tenant_id="tenant_a",
    )
    tenant_b = memory.add_long_term_memory(
        "preference",
        "用户偏好: 每次 30 分钟",
        "tenant b",
        {},
        user_id="user_a",
        tenant_id="tenant_b",
    )

    assert len(memory.long_term_memories) == 3
    assert user_a.user_id == "user_a"
    assert user_b.user_id == "user_b"
    assert tenant_b.tenant_id == "tenant_b"
    assert memory.snapshot(user_id="user_a", tenant_id="tenant_a").long_term_memories == [user_a]
    assert memory.snapshot(user_id="user_b", tenant_id="tenant_a").long_term_memories == [user_b]
    assert memory.snapshot(user_id="user_a", tenant_id="tenant_b").long_term_memories == [tenant_b]


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


def test_agent_saves_and_reads_long_term_memory_in_identity_namespace() -> None:
    memory = MemoryStore()
    policy = RuleBasedMemoryPolicy()
    agent_a = Agent(
        planner=RuleBasedPlanner(),
        tools=ToolRegistry(memory, policy),
        memory=memory,
        memory_policy=policy,
        identity=RuntimeIdentity(user_id="user_a", tenant_id="tenant_a"),
    )
    agent_b = Agent(
        planner=RuleBasedPlanner(),
        tools=ToolRegistry(memory, policy),
        memory=memory,
        memory_policy=policy,
        identity=RuntimeIdentity(user_id="user_b", tenant_id="tenant_a"),
    )

    agent_a.run("以后安排学习任务时，每次控制在30分钟以内")
    result_b = agent_b.run("帮我安排 agentic memory 的学习计划")

    assert memory.long_term_memories[0].user_id == "user_a"
    assert memory.long_term_memories[0].tenant_id == "tenant_a"
    assert result_b["memory"]["longTermMemories"] == []


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


def test_semantic_merge_updates_user_tech_stack_memory() -> None:
    memory = MemoryStore()

    first = memory.add_long_term_memory(
        "user_profile",
        "用户技术栈: Node.js、React、Codex",
        "old profile",
        {"user_profile": 5, "stability": 5},
    )
    second = memory.add_long_term_memory(
        "user_profile",
        "用户技术栈: Python、FastAPI、React",
        "new profile",
        {"user_profile": 5, "stability": 5, "explicit_memory_intent": 5},
    )

    assert first == second
    assert len(memory.long_term_memories) == 1
    assert second.text == "用户技术栈: Python、FastAPI、React"
    assert second.reason == "new profile"
    assert second.merged_from == ["用户技术栈: Node.js、React、Codex"]
    assert second.importance >= first.importance


def test_task_state_gets_default_expiry_and_can_be_archived() -> None:
    memory = MemoryStore()
    record = memory.add_long_term_memory(
        "task_state",
        "任务状态: 正在实现 Memory Lifecycle",
        "temporary task state",
        {"task_continuity": 5},
    )

    assert record.expires_at is not None

    archived = memory.archive_expired_long_term_memories("2100-01-01T00:00:00+00:00")

    assert archived == [record]
    assert record.status == "archived"
    assert record.archive_reason == "expired"
    assert memory.snapshot().long_term_memories == []


def test_snapshot_auto_archives_expired_task_state() -> None:
    memory = MemoryStore()
    record = memory.add_long_term_memory("task_state", "任务状态: 临时任务", "test", {})
    record.expires_at = "2000-01-01T00:00:00+00:00"

    snapshot = memory.snapshot()

    assert snapshot.long_term_memories == []
    assert record.status == "archived"
    assert record.archive_reason == "expired"


def test_prune_long_term_memories_archives_low_value_active_memories() -> None:
    memory = MemoryStore()
    high = memory.add_long_term_memory(
        "user_profile",
        "用户技术栈: Python",
        "important",
        {"user_profile": 5, "stability": 5},
    )
    low = memory.add_long_term_memory("long_term_note", "长期笔记: 临时想法", "low", {})
    medium = memory.add_long_term_memory("preference", "用户偏好: 学习任务控制在 30 分钟以内", "medium", {})
    memory.touch_long_term_memory(medium.id)

    archived = memory.prune_long_term_memories(max_active=2)

    assert archived == [low]
    assert low.status == "archived"
    assert high.status == "active"
    assert medium.status == "active"
    assert [item.id for item in memory.snapshot().long_term_memories] == [high.id, medium.id]


def test_memory_embedding_search_ranks_relevant_active_memories() -> None:
    memory = MemoryStore()
    tech_stack = memory.add_long_term_memory(
        "user_profile",
        "用户技术栈: Python、FastAPI、React",
        "profile",
        {"user_profile": 5},
    )
    study_preference = memory.add_long_term_memory(
        "preference",
        "用户偏好: 学习任务控制在 30 分钟以内",
        "study",
        {"user_preference": 5},
    )
    coffee = memory.add_long_term_memory("long_term_note", "长期笔记: 喜欢咖啡", "note", {})
    memory.archive_long_term_memory(coffee.id, "irrelevant")

    project_matches = memory.search_long_term_memories("我要写 FastAPI React 项目", limit=1)
    study_matches = memory.search_long_term_memories("帮我安排 agentic 学习计划", limit=1)

    assert project_matches == [tech_stack]
    assert study_matches == [study_preference]


def test_snapshot_can_rank_and_limit_long_term_memories_by_query() -> None:
    memory = MemoryStore()
    memory.add_long_term_memory("preference", "用户偏好: 学习任务控制在 30 分钟以内", "study", {})
    tech_stack = memory.add_long_term_memory("user_profile", "用户技术栈: Python、FastAPI、React", "profile", {})
    memory.add_long_term_memory("long_term_note", "长期笔记: 喜欢咖啡", "note", {})

    snapshot = memory.snapshot(query="我要写 React 项目", max_long_term_memories=1)

    assert snapshot.long_term_memories == [tech_stack]


def test_agent_planner_receives_query_ranked_memory_snapshot() -> None:
    memory = MemoryStore()
    memory.add_long_term_memory("preference", "用户偏好: 学习任务控制在 30 分钟以内", "study", {})
    tech_stack = memory.add_long_term_memory("user_profile", "用户技术栈: Python、FastAPI、React", "profile", {})
    policy = RuleBasedMemoryPolicy()
    planner = CaptureSnapshotPlanner()
    agent = Agent(
        planner=planner,
        tools=ToolRegistry(memory, policy),
        memory=memory,
        memory_policy=policy,
    )

    agent.run("帮我计算 1 + 1, 我后面要写 React 项目")

    assert planner.first_snapshot_ids[0] == tech_stack.id


def test_update_long_term_memory_importance_clamps_to_valid_range() -> None:
    memory = MemoryStore()
    record = memory.add_long_term_memory("long_term_note", "长期笔记: x", "test", {})

    updated = memory.update_long_term_memory_importance(record.id, 999)

    assert updated.importance == 100


class CaptureSnapshotPlanner:
    def __init__(self) -> None:
        self.first_snapshot_ids: list[str] = []

    def next(self, context: PlannerContext) -> Action:
        if not self.first_snapshot_ids:
            self.first_snapshot_ids = [memory.id for memory in context.memory_snapshot.long_term_memories]
        return Action.final("done", reason="test", source="test")
