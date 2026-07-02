from __future__ import annotations

from agentic_core.agent import Agent
from agentic_core.memory import MemoryStore
from agentic_core.memory_policy import RuleBasedMemoryPolicy
from agentic_core.middleware import (
    ApprovalMiddleware,
    CostAccountingMiddleware,
    MiddlewarePipeline,
    ToolCallContext,
)
from agentic_core.planner import RuleBasedPlanner
from agentic_core.schemas import Action
from agentic_core.tools import ToolRegistry, ToolSpec


def test_cost_accounting_middleware_records_tool_cost() -> None:
    context = ToolCallContext(
        run_id="run_1",
        step=1,
        action=Action.tool("expensive.tool", {}),
        tool=ToolSpec(
            name="expensive.tool",
            description="test",
            execute=lambda data: data,
            input_schema={},
            side_effect="read",
            cost_units=7,
        ),
    )

    observation = CostAccountingMiddleware().before_tool(context)

    assert observation is None
    assert context.metadata["costUnits"] == 7


def test_approval_middleware_blocks_unapproved_tool() -> None:
    context = ToolCallContext(
        run_id="run_1",
        step=1,
        action=Action.tool("danger.delete", {}),
        tool=ToolSpec(
            name="danger.delete",
            description="danger",
            execute=lambda data: data,
            input_schema={},
            side_effect="write",
            requires_approval=True,
        ),
    )

    observation = ApprovalMiddleware().before_tool(context)

    assert observation is not None
    assert observation.ok is False
    assert "requires approval" in observation.error


def test_agent_blocks_tool_that_requires_approval() -> None:
    memory = MemoryStore()
    policy = RuleBasedMemoryPolicy()
    tools = ToolRegistry(memory, policy)
    executed = {"value": False}

    def execute_danger(input_data: dict) -> dict:
        executed["value"] = True
        return {"deleted": True}

    tools._register(
        "danger.delete",
        "Dangerous write.",
        execute_danger,
        side_effect="write",
        requires_approval=True,
        risk_level="high",
    )
    agent = Agent(
        planner=DangerPlanner(),
        tools=tools,
        memory=memory,
        memory_policy=policy,
        responder=None,
    )

    result = agent.run_typed("删除全部数据")

    assert executed["value"] is False
    assert result.trace[0].action.tool_name == "danger.delete"
    assert result.trace[0].observation.ok is False
    assert "requires approval" in str(result.trace[0].observation.error)
    assert "执行失败" in result.answer


def test_empty_middleware_pipeline_keeps_tool_execution_unchanged() -> None:
    memory = MemoryStore()
    policy = RuleBasedMemoryPolicy()
    agent = Agent(
        planner=RuleBasedPlanner(),
        tools=ToolRegistry(memory, policy),
        memory=memory,
        memory_policy=policy,
        middleware_pipeline=MiddlewarePipeline([]),
    )

    result = agent.run_typed("帮我计算 128 * 7")

    assert result.trace[0].observation.ok is True
    assert result.trace[0].observation.output["result"] == 896


class DangerPlanner:
    def next(self, context: object) -> Action:
        if not getattr(context, "trace"):
            return Action.tool("danger.delete", {}, reason="test", source="test")
        return Action.final("done", reason="test", source="test")
