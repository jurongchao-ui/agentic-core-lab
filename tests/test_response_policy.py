from __future__ import annotations

from typing import Any

from agentic_core.agent import Agent
from agentic_core.memory import MemoryStore
from agentic_core.memory_policy import RuleBasedMemoryPolicy
from agentic_core.planner import RuleBasedPlanner
from agentic_core.response_policy import ResponseContext, ResponsePolicy
from agentic_core.schemas import MemoryDecision
from agentic_core.tools import ToolRegistry


class StubResponder:
    def reply(self, goal: str, memory_snapshot: dict[str, Any]) -> str:
        return f"自然回复: {goal}"


def decision(
    save: bool = False,
    text: str = "",
    reason: str = "不保存",
    scores: dict[str, int] | None = None,
    needs_clarification: bool = False,
    question: str | None = None,
) -> MemoryDecision:
    return MemoryDecision(
        save=save,
        memory_type="preference" if save else "none",
        text=text,
        reason=reason,
        scores=scores or {},
        needs_clarification=needs_clarification,
        clarification_question=question,
    )


def context(
    memory_decision: MemoryDecision,
    trace: list[dict[str, Any]] | None = None,
    saved_memories: list[dict[str, Any]] | None = None,
    planner_answer: str | None = None,
    incomplete_reason: str | None = None,
    responder: Any | None = None,
) -> ResponseContext:
    return ResponseContext(
        goal="测试目标",
        memory_decision=memory_decision,
        saved_memories=saved_memories or [],
        trace=trace or [],
        planner_answer=planner_answer,
        incomplete_reason=incomplete_reason,
        memory_snapshot={"notes": [], "todos": [], "longTermMemories": []},
        responder=responder,
    )


def test_clarification_global_intercept() -> None:
    policy = ResponsePolicy()
    response = policy.decide(
        context(
            decision(
                needs_clarification=True,
                question="可以，请告诉我你的技术栈具体包括哪些？",
            ),
            trace=[calculator_success()],
        )
    )

    assert response.text == "可以，请告诉我你的技术栈具体包括哪些？"
    assert response.tiers == ["clarification"]
    assert "计算结果" not in response.text


def test_local_safety_can_be_combined_with_safe_tool_result() -> None:
    policy = ResponsePolicy()
    response = policy.decide(
        context(
            decision(reason="包含敏感信息风险,不进入长期记忆。", scores={"sensitivity_risk": 5}),
            trace=[calculator_success()],
        )
    )

    assert "不适合进入长期记忆" in response.text
    assert "计算结果是 896" in response.text
    assert response.tiers == ["local_safety", "tool_result_summary"]


def test_local_safety_fires_from_structured_signal_without_keyword() -> None:
    """输入 decision 的 reason 不含“敏感”,仅靠 sensitivity_risk 信号也能命中 safety 档。"""
    policy = ResponsePolicy()
    response = policy.decide(
        context(decision(reason="按策略不保存", scores={"sensitivity_risk": 5}))
    )

    assert "不适合进入长期记忆" in response.text
    assert response.tiers == ["local_safety"]


def test_memory_confirmation_uses_saved_memories_list() -> None:
    policy = ResponsePolicy()
    response = policy.decide(
        context(
            decision(save=True, text="用户偏好: 每次学习控制在30分钟以内"),
            saved_memories=[
                {"text": "用户偏好: 每次学习控制在30分钟以内"},
                {"text": "用户技术栈: Python、FastAPI、React"},
            ],
        )
    )

    assert "已记住" in response.text
    assert "每次学习控制在30分钟以内" in response.text
    assert "Python、FastAPI、React" in response.text
    assert response.tiers == ["memory_confirmation"]


def test_tool_success_summary_comes_from_observation() -> None:
    policy = ResponsePolicy()
    response = policy.decide(
        context(
            decision(),
            trace=[
                calculator_success(),
                {
                    "step": 2,
                    "action": {"toolName": "note.add"},
                    "observation": {"ok": True, "output": {"text": "计算 128 * 7 = 896"}},
                },
            ],
        )
    )

    assert "计算结果是 896" in response.text
    assert "已记录学习笔记" in response.text
    assert response.tiers == ["tool_result_summary"]


def test_failed_calculation_blocks_dependent_note_confirmation() -> None:
    policy = ResponsePolicy()
    response = policy.decide(
        ResponseContext(
            goal="帮我算 128 / 0，然后记成笔记",
            memory_decision=decision(),
            saved_memories=[],
            trace=[
                {
                    "step": 1,
                    "action": {"toolName": "calculator"},
                    "observation": {"ok": False, "error": "division by zero"},
                },
                {
                    "step": 2,
                    "action": {"toolName": "note.add"},
                    "observation": {"ok": True, "output": {"text": "学习笔记: 帮我算 128 / 0"}},
                },
            ],
            planner_answer=None,
            incomplete_reason=None,
            memory_snapshot={"notes": [], "todos": [], "longTermMemories": []},
        )
    )

    assert "计算失败" in response.text
    assert "没有记录学习笔记" in response.text
    assert "已记录学习笔记" not in response.text
    assert response.tiers == ["failure_incomplete"]


def test_normal_turn_uses_responder() -> None:
    policy = ResponsePolicy()
    response = policy.decide(context(decision(), responder=StubResponder()))

    assert response.text == "自然回复: 测试目标"
    assert response.tiers == ["normal_responder"]


def test_agent_does_not_write_note_after_failed_dependent_calculation() -> None:
    memory = MemoryStore()
    memory_policy = RuleBasedMemoryPolicy()
    agent = Agent(
        planner=RuleBasedPlanner(),
        tools=ToolRegistry(memory, memory_policy),
        memory=memory,
        memory_policy=memory_policy,
    )

    result = agent.run("帮我算 128 / 0，然后记成笔记")

    assert "计算失败" in result["answer"]
    assert "没有记录学习笔记" in result["answer"]
    assert result["memory"]["notes"] == []
    assert len(result["trace"]) == 1


def calculator_success() -> dict[str, Any]:
    return {
        "step": 1,
        "action": {"toolName": "calculator"},
        "observation": {
            "ok": True,
            "output": {"expression": "128 * 7", "result": 896},
        },
    }
