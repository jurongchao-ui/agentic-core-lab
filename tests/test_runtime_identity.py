from __future__ import annotations

from agentic_core.agent import Agent
from agentic_core.memory import MemoryStore
from agentic_core.memory_policy import RuleBasedMemoryPolicy
from agentic_core.middleware import ToolCallContext, ToolGovernanceMiddleware, ToolGovernancePolicy
from agentic_core.planner import RuleBasedPlanner
from agentic_core.runtime_context import RuntimeIdentity, build_runtime_identity_from_env
from agentic_core.schemas import Action
from agentic_core.tools import ToolRegistry, ToolSpec


def test_runtime_identity_to_dict_is_stable() -> None:
    identity = RuntimeIdentity(
        user_id="user_1",
        tenant_id="tenant_a",
        roles={"admin", "developer"},
        permission_scopes={"memory:note:write", "tool:calculator:read"},
    )

    assert identity.to_dict() == {
        "userId": "user_1",
        "tenantId": "tenant_a",
        "roles": ["admin", "developer"],
        "permissionScopes": ["memory:note:write", "tool:calculator:read"],
    }


def test_build_runtime_identity_from_env(monkeypatch) -> None:
    monkeypatch.setenv("AGENTIC_USER_ID", "user_1")
    monkeypatch.setenv("AGENTIC_TENANT_ID", "tenant_a")
    monkeypatch.setenv("AGENTIC_ROLES", "developer,admin")
    monkeypatch.setenv("AGENTIC_PERMISSION_SCOPES", "tool:calculator:read,memory:note:write")

    identity = build_runtime_identity_from_env()

    assert identity.user_id == "user_1"
    assert identity.tenant_id == "tenant_a"
    assert identity.roles == {"developer", "admin"}
    assert identity.permission_scopes == {"tool:calculator:read", "memory:note:write"}


def test_agent_result_and_events_include_identity() -> None:
    memory = MemoryStore()
    policy = RuleBasedMemoryPolicy()
    identity = RuntimeIdentity(user_id="user_1", tenant_id="tenant_a", roles={"developer"})
    agent = Agent(
        planner=RuleBasedPlanner(),
        tools=ToolRegistry(memory, policy),
        memory=memory,
        memory_policy=policy,
        identity=identity,
    )

    result = agent.run_typed("帮我计算 128 * 7")

    assert result.identity == identity
    assert result.to_dict()["identity"]["tenantId"] == "tenant_a"
    assert result.events[0].payload["identity"]["userId"] == "user_1"
    assert result.events[-1].payload["identity"]["tenantId"] == "tenant_a"
    assert result.trace[0].observation.metadata["identity"]["userId"] == "user_1"


def test_identity_permission_scopes_limit_tool_execution() -> None:
    identity = RuntimeIdentity(
        user_id="user_1",
        tenant_id="tenant_a",
        roles={"developer"},
        permission_scopes={"tool:calculator:read"},
    )
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
        identity=identity,
    )

    observation = ToolGovernanceMiddleware().before_tool(context)

    assert observation is not None
    assert observation.ok is False
    assert "permission scope not allowed" in str(observation.error)


def test_tenant_budget_is_isolated_by_tenant_and_run() -> None:
    middleware = ToolGovernanceMiddleware(ToolGovernancePolicy(max_cost_units_per_run=3))
    tool = ToolSpec(
        name="expensive.tool",
        description="expensive",
        execute=lambda data: data,
        input_schema={},
        side_effect="read",
        cost_units=2,
    )
    tenant_a_first = ToolCallContext(
        run_id="run_1",
        step=1,
        action=Action.tool("expensive.tool", {}),
        tool=tool,
        identity=RuntimeIdentity(user_id="u1", tenant_id="tenant_a"),
    )
    tenant_a_second = ToolCallContext(
        run_id="run_1",
        step=2,
        action=Action.tool("expensive.tool", {}),
        tool=tool,
        identity=RuntimeIdentity(user_id="u1", tenant_id="tenant_a"),
    )
    tenant_b_first = ToolCallContext(
        run_id="run_1",
        step=1,
        action=Action.tool("expensive.tool", {}),
        tool=tool,
        identity=RuntimeIdentity(user_id="u2", tenant_id="tenant_b"),
    )

    assert middleware.before_tool(tenant_a_first) is None
    assert middleware.before_tool(tenant_a_second) is not None
    assert middleware.before_tool(tenant_b_first) is None
    assert tenant_a_first.metadata["budgetKey"] == "tenant_a:run_1"
    assert tenant_b_first.metadata["budgetKey"] == "tenant_b:run_1"
