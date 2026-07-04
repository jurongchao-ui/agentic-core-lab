from __future__ import annotations

from agentic_core.runtime.agent import Agent
from agentic_core.memory.store import MemoryStore
from agentic_core.policies.memory import RuleBasedMemoryPolicy
from agentic_core.policies.planner import RuleBasedPlanner, extract_study_max_minutes, extract_study_topic
from agentic_core.tools.registry import ToolRegistry


def test_study_plan_tool_respects_max_minutes() -> None:
    memory = MemoryStore()
    policy = RuleBasedMemoryPolicy()
    tools = ToolRegistry(memory, policy)

    output = tools.execute("study.plan", {"topic": "agentic memory", "max_minutes": 30})

    assert output["topic"] == "agentic memory"
    assert output["maxMinutes"] == 30
    assert len(output["steps"]) == 3
    assert all("分钟" in step for step in output["steps"])


def test_rule_planner_extracts_study_topic() -> None:
    assert extract_study_topic("帮我安排 agentic memory 的学习计划") == "agentic memory"


def test_rule_planner_does_not_treat_preference_as_study_plan() -> None:
    assert extract_study_topic("以后安排学习任务时，每次控制在30分钟以内") is None


def test_rule_planner_reads_study_minutes_from_long_term_memory() -> None:
    memory = MemoryStore()
    memory.add_long_term_memory(
        memory_type="preference",
        text="用户偏好: 以后安排学习任务时，每次控制在30分钟以内",
        reason="test",
        scores={},
    )

    assert extract_study_max_minutes("帮我安排 agentic memory 的学习计划", memory.snapshot()) == 30


def test_agent_uses_long_term_memory_for_study_plan() -> None:
    memory = MemoryStore()
    policy = RuleBasedMemoryPolicy()
    memory.add_long_term_memory(
        memory_type="preference",
        text="用户偏好: 以后安排学习任务时，每次控制在30分钟以内",
        reason="test",
        scores={},
    )
    agent = Agent(
        planner=RuleBasedPlanner(),
        tools=ToolRegistry(memory, policy),
        memory=memory,
        memory_policy=policy,
    )

    result = agent.run_typed("帮我安排 agentic memory 的学习计划")
    study_plan_steps = [step for step in result.trace if step.action.tool_name == "study.plan"]

    assert result.status == "completed"
    assert len(study_plan_steps) == 1
    assert study_plan_steps[0].action.input["max_minutes"] == 30
    assert "总时长不超过 30 分钟" in result.answer
