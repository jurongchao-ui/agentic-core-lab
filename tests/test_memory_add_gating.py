from __future__ import annotations

from agentic_core.agent import Agent
from agentic_core.memory import MemoryStore
from agentic_core.memory_policy import RuleBasedMemoryPolicy
from agentic_core.planner import RuleBasedPlanner
from agentic_core.tools import ToolRegistry


def build_registry() -> tuple[ToolRegistry, MemoryStore]:
    memory = MemoryStore()
    registry = ToolRegistry(memory, RuleBasedMemoryPolicy())
    return registry, memory


def test_rejects_sensitive() -> None:
    """安全回归: 模型即使调用 memory.add,敏感信息也不能进入长期记忆。"""
    registry, memory = build_registry()
    result = registry.execute("memory.add", {"text": "我的密码是 abcd1234"})
    assert result["saved"] is False
    assert memory.long_term_memories == []


def test_rejects_low_value() -> None:
    registry, memory = build_registry()
    result = registry.execute("memory.add", {"text": "我今天有点累"})
    assert result["saved"] is False
    assert memory.long_term_memories == []


def test_saves_worthy() -> None:
    registry, memory = build_registry()
    result = registry.execute(
        "memory.add", {"text": "以后安排学习任务时，每次控制在30分钟以内"}
    )
    assert result["saved"] is True
    assert result["memory"]["type"] == "preference"
    assert len(memory.long_term_memories) == 1


def test_ignores_model_supplied_scores() -> None:
    """模型自带的高分不能强行让低价值文本保存。"""
    registry, memory = build_registry()
    result = registry.execute(
        "memory.add",
        {"text": "我今天有点累", "scores": {"future_relevance": 99}},
    )
    assert result["saved"] is False
    assert memory.long_term_memories == []


def test_agent_level_save_still_works() -> None:
    """回归: Agent 层对用户输入的记忆评估(前门)未被破坏。"""
    memory = MemoryStore()
    policy = RuleBasedMemoryPolicy()
    agent = Agent(
        planner=RuleBasedPlanner(),
        tools=ToolRegistry(memory, policy),
        memory=memory,
        memory_policy=policy,
    )
    result = agent.run("以后安排学习任务时，每次控制在30分钟以内")
    assert result["memoryDecision"]["save"] is True
