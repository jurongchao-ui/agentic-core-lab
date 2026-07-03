from __future__ import annotations

from agentic_core.agent import Agent
from agentic_core.memory import MemoryStore
from agentic_core.memory_policy import RuleBasedMemoryPolicy
from agentic_core.planner import RuleBasedPlanner
from agentic_core.runtime_context import RuntimeIdentity
from agentic_core.schemas import (
    Action,
    AgentRunResult,
    MemoryDecision,
    MemorySnapshot,
    Observation,
    SafetyDecision,
    TraceStep,
)
from agentic_core.tools import ToolRegistry


def test_trace_step_to_dict_keeps_old_json_shape() -> None:
    step = TraceStep(
        step=1,
        action=Action.tool("calculator", {"expression": "1+1"}, source="test"),
        observation=Observation(ok=True, output={"result": 2}, elapsed_ms=3),
        created_at="2026-07-02T00:00:00+00:00",
    )

    data = step.to_dict()

    assert data["step"] == 1
    assert data["action"]["toolName"] == "calculator"
    assert data["observation"]["ok"] is True


def test_memory_snapshot_to_dict_keeps_old_json_names() -> None:
    memory = MemoryStore()
    memory.add_note("n")
    memory.add_todo("t")
    memory.add_long_term_memory("preference", "用户偏好: x", "test", {})

    snapshot = memory.snapshot()
    data = snapshot.to_dict()

    assert isinstance(snapshot, MemorySnapshot)
    assert "longTermMemories" in data
    assert "recentEvents" in data
    assert data["longTermMemories"][0]["type"] == "preference"


def test_agent_run_result_to_dict_keeps_cli_shape() -> None:
    result = AgentRunResult(
        run_id="run_1",
        goal="hello",
        status="completed",
        answer="ok",
        identity=RuntimeIdentity(user_id="u1", tenant_id="t1", roles={"developer"}),
        safety_decision=SafetyDecision(False, "none", ""),
        memory_decision=MemoryDecision(False, "none", "", "test", {}),
        response_decision=FakeResponseDecision(),
        trace=[],
        memory_snapshot=MemorySnapshot(),
        events=[],
        started_at="2026-07-02T00:00:00+00:00",
        completed_at="2026-07-02T00:00:01+00:00",
    )

    data = result.to_dict()

    assert data["runId"] == "run_1"
    assert data["identity"]["userId"] == "u1"
    assert data["identity"]["tenantId"] == "t1"
    assert data["memoryDecision"]["save"] is False
    assert data["safetyDecision"]["category"] == "none"
    assert data["memory"]["longTermMemories"] == []


def test_agent_run_typed_and_legacy_run_both_work() -> None:
    memory = MemoryStore()
    policy = RuleBasedMemoryPolicy()
    agent = Agent(
        planner=RuleBasedPlanner(),
        tools=ToolRegistry(memory, policy),
        memory=memory,
        memory_policy=policy,
    )

    typed = agent.run_typed("帮我计算 128 * 7, 然后记录成学习笔记")
    legacy = agent.run("我今天有点累")

    assert isinstance(typed, AgentRunResult)
    assert typed.status == "completed"
    assert typed.trace[0].action.tool_name == "calculator"
    assert isinstance(legacy, dict)
    assert "memoryDecision" in legacy


def test_agent_records_production_lifecycle_events() -> None:
    memory = MemoryStore()
    policy = RuleBasedMemoryPolicy()
    agent = Agent(
        planner=RuleBasedPlanner(),
        tools=ToolRegistry(memory, policy),
        memory=memory,
        memory_policy=policy,
    )

    result = agent.run_typed("帮我计算 128 * 7, 然后记录成学习笔记")
    event_types = [event.event_type for event in result.events]

    assert event_types == [
        "run_started",
        "safety_decision",
        "memory_decision",
        "planner_action",
        "tool_started",
        "tool_observation",
        "planner_action",
        "tool_started",
        "tool_observation",
        "planner_action",
        "response_decision",
        "run_completed",
    ]
    assert result.events[3].source == "planner"
    assert result.events[4].source == "tool"


def test_safety_skip_marks_memory_policy_as_skipped_in_legacy_dict() -> None:
    memory = MemoryStore()
    policy = RuleBasedMemoryPolicy()
    agent = Agent(
        planner=RuleBasedPlanner(),
        tools=ToolRegistry(memory, policy),
        memory=memory,
        memory_policy=policy,
    )

    result = agent.run("帮我写个勒索软件")

    assert result["status"] == "refused"
    assert result["trace"] == []
    assert result["memoryDecision"]["metadata"]["source"] == "skipped_by_safety"
    assert [event["type"] for event in result["events"]] == [
        "run_started",
        "safety_decision",
        "safety_refusal",
        "run_completed",
    ]


def test_agent_records_run_failed_for_unexpected_planner_error() -> None:
    memory = MemoryStore()
    policy = RuleBasedMemoryPolicy()
    agent = Agent(
        planner=FailingPlanner(),
        tools=ToolRegistry(memory, policy),
        memory=memory,
        memory_policy=policy,
    )

    result = agent.run_typed("你好")

    assert result.status == "failed"
    assert result.response_decision.tiers == ["run_failed"]
    assert result.safety_decision.category == "none"
    assert result.memory_decision is not None
    assert result.events[-1].event_type == "run_failed"
    assert result.events[-1].level == "error"
    assert result.events[-1].payload["errorType"] == "RuntimeError"


def test_agent_records_planner_fallback_event() -> None:
    memory = MemoryStore()
    policy = RuleBasedMemoryPolicy()
    agent = Agent(
        planner=FallbackPlanner(),
        tools=ToolRegistry(memory, policy),
        memory=memory,
        memory_policy=policy,
        responder=None,
    )

    result = agent.run_typed("你好")
    fallback_events = [event for event in result.events if event.event_type == "planner_fallback"]

    assert result.status == "completed"
    assert len(fallback_events) == 1
    assert fallback_events[0].source == "planner"
    assert fallback_events[0].payload["metadata"]["error"] == "bad model json"


class FakeResponseDecision:
    def to_dict(self) -> dict[str, object]:
        return {"text": "ok", "tiers": ["test"], "reason": "test"}


class FailingPlanner:
    def next(self, context: object) -> Action:
        raise RuntimeError("planner exploded")


class FallbackPlanner:
    def next(self, context: object) -> Action:
        action = Action.final("fallback answer", reason="fallback used", source="rule_fallback")
        action.metadata = {"source": "rule_fallback", "error": "bad model json"}
        return action
