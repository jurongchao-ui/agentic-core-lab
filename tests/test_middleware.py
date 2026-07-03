from __future__ import annotations

import time

from agentic_core.agent import Agent
from agentic_core.memory import MemoryStore
from agentic_core.memory_policy import RuleBasedMemoryPolicy
from agentic_core.middleware import (
    ApprovalMiddleware,
    CostAccountingMiddleware,
    MiddlewarePipeline,
    ToolCallContext,
    ToolGovernanceMiddleware,
    ToolGovernancePolicy,
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
    assert "requires approval" in str(observation.error)


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
    assert result.trace[0].observation.metadata["shortCircuited"] is True
    assert result.trace[0].observation.metadata["requiresApproval"] is True
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
    assert result.trace[0].observation.metadata["toolName"] == "calculator"


def test_pipeline_retries_failed_tool_using_tool_metadata() -> None:
    context = ToolCallContext(
        run_id="run_1",
        step=1,
        action=Action.tool("flaky.tool", {}),
        tool=ToolSpec(
            name="flaky.tool",
            description="flaky",
            execute=lambda data: data,
            input_schema={},
            side_effect="read",
            retry_count=1,
        ),
    )
    calls = {"count": 0}

    def execute() -> dict:
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("temporary failure")
        return {"ok": True}

    observation = MiddlewarePipeline.default().execute_tool(context, execute)

    assert observation.ok is True
    assert observation.output == {"ok": True}
    assert calls["count"] == 2
    assert observation.metadata["attempts"] == 2
    assert observation.metadata["retryCount"] == 1


def test_pipeline_times_out_tool_using_tool_metadata() -> None:
    context = ToolCallContext(
        run_id="run_1",
        step=1,
        action=Action.tool("slow.tool", {}),
        tool=ToolSpec(
            name="slow.tool",
            description="slow",
            execute=lambda data: data,
            input_schema={},
            side_effect="read",
            timeout_ms=1,
        ),
    )

    def execute() -> dict:
        time.sleep(0.05)
        return {"done": True}

    observation = MiddlewarePipeline.default().execute_tool(context, execute)

    assert observation.ok is False
    assert "timed out" in str(observation.error)
    assert observation.metadata["timeoutMs"] == 1
    assert observation.metadata["attempts"] == 1


def test_pipeline_generates_stable_idempotency_key() -> None:
    tool = ToolSpec(
        name="note.add",
        description="write",
        execute=lambda data: data,
        input_schema={},
        side_effect="write",
        version="1.0",
    )
    first_context = ToolCallContext(
        run_id="run_1",
        step=1,
        action=Action.tool("note.add", {"text": "hello"}),
        tool=tool,
    )
    second_context = ToolCallContext(
        run_id="run_1",
        step=1,
        action=Action.tool("note.add", {"text": "hello"}),
        tool=tool,
    )

    first = MiddlewarePipeline.default().execute_tool(first_context, lambda: {"saved": True})
    second = MiddlewarePipeline.default().execute_tool(second_context, lambda: {"saved": True})

    assert first.metadata["idempotencyKey"] == second.metadata["idempotencyKey"]
    assert str(first.metadata["idempotencyKey"]).startswith("tool_")


def test_governance_middleware_denies_disallowed_permission_scope() -> None:
    context = ToolCallContext(
        run_id="run_1",
        step=1,
        action=Action.tool("note.add", {"text": "hello"}),
        tool=ToolSpec(
            name="note.add",
            description="write note",
            execute=lambda data: data,
            input_schema={},
            side_effect="write",
            permission_scope="memory:note:write",
        ),
    )
    middleware = ToolGovernanceMiddleware(
        ToolGovernancePolicy(allowed_permission_scopes={"tool:calculator:read"})
    )

    observation = middleware.before_tool(context)

    assert observation is not None
    assert observation.ok is False
    assert "permission scope not allowed" in str(observation.error)


def test_governance_middleware_blocks_denied_permission_scope() -> None:
    context = ToolCallContext(
        run_id="run_1",
        step=1,
        action=Action.tool("todo.list", {}),
        tool=ToolSpec(
            name="todo.list",
            description="list todo",
            execute=lambda data: data,
            input_schema={},
            side_effect="read",
            permission_scope="memory:todo:read",
        ),
    )
    middleware = ToolGovernanceMiddleware(
        ToolGovernancePolicy(denied_permission_scopes={"memory:todo:read"})
    )

    observation = middleware.before_tool(context)

    assert observation is not None
    assert "permission scope denied" in str(observation.error)


def test_default_pipeline_requires_approval_for_high_risk_tool() -> None:
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
            risk_level="high",
            requires_approval=False,
        ),
    )
    executed = {"value": False}

    def execute() -> dict:
        executed["value"] = True
        return {"deleted": True}

    observation = MiddlewarePipeline.default().execute_tool(context, execute)

    assert executed["value"] is False
    assert observation.ok is False
    assert "risk level high requires approval" in str(observation.error)
    assert observation.metadata["approvalRequired"] is True
    assert observation.metadata["shortCircuited"] is True


def test_governance_middleware_can_require_approval_for_write_side_effect() -> None:
    context = ToolCallContext(
        run_id="run_1",
        step=1,
        action=Action.tool("note.add", {"text": "hello"}),
        tool=ToolSpec(
            name="note.add",
            description="write note",
            execute=lambda data: data,
            input_schema={},
            side_effect="write",
            risk_level="medium",
        ),
    )
    middleware = ToolGovernanceMiddleware(
        ToolGovernancePolicy(require_approval_for_side_effects={"write"})
    )

    observation = middleware.before_tool(context)

    assert observation is not None
    assert observation.ok is False
    assert "side effect write requires approval" in str(observation.error)


def test_governance_budget_limits_cost_units_per_run() -> None:
    middleware = ToolGovernanceMiddleware(ToolGovernancePolicy(max_cost_units_per_run=3))
    tool = ToolSpec(
        name="expensive.tool",
        description="expensive",
        execute=lambda data: data,
        input_schema={},
        side_effect="read",
        cost_units=2,
    )
    first = ToolCallContext(run_id="run_1", step=1, action=Action.tool("expensive.tool", {}), tool=tool)
    second = ToolCallContext(run_id="run_1", step=2, action=Action.tool("expensive.tool", {}), tool=tool)

    first_observation = middleware.before_tool(first)
    second_observation = middleware.before_tool(second)

    assert first_observation is None
    assert first.metadata["budgetUsedAfter"] == 2
    assert second_observation is not None
    assert "tool budget exceeded" in str(second_observation.error)
    assert second.metadata["budgetUsedBefore"] == 2
    assert second.metadata["budgetUsedAfter"] == 4


def test_governance_approval_allows_high_risk_tool_when_context_is_approved() -> None:
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
            risk_level="high",
        ),
        metadata={"approved": True},
    )

    observation = ToolGovernanceMiddleware().before_tool(context)

    assert observation is None
    assert context.metadata.get("approvalRequired") is None


class DangerPlanner:
    def next(self, context: object) -> Action:
        if not getattr(context, "trace"):
            return Action.tool("danger.delete", {}, reason="test", source="test")
        return Action.final("done", reason="test", source="test")
