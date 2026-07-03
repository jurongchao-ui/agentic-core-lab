from __future__ import annotations

from agentic_core.memory import MemoryStore
from agentic_core.memory_policy import RuleBasedMemoryPolicy
from agentic_core.tools import ToolRegistry


def build_registry() -> ToolRegistry:
    return ToolRegistry(MemoryStore(), RuleBasedMemoryPolicy())


def test_tool_list_exposes_production_metadata() -> None:
    calculator = next(tool for tool in build_registry().list() if tool["name"] == "calculator")

    assert calculator["permissionScope"] == "tool:calculator:read"
    assert calculator["timeoutMs"] == 500
    assert calculator["costUnits"] == 1
    assert calculator["retryCount"] == 0
    assert calculator["riskLevel"] == "low"
    assert calculator["requiresApproval"] is False
    assert calculator["version"] == "1.0"


def test_write_tools_are_marked_with_write_scope_and_sensitive_guard() -> None:
    tools = {tool["name"]: tool for tool in build_registry().list()}

    assert tools["note.add"]["sideEffect"] == "write"
    assert tools["note.add"]["permissionScope"] == "memory:note:write"
    assert tools["note.add"]["riskLevel"] == "medium"
    assert tools["note.add"]["guardSensitive"] is True
    assert tools["todo.add"]["permissionScope"] == "memory:todo:write"
    assert tools["memory.add"]["permissionScope"] == "memory:long_term:write"


def test_custom_tool_gets_default_metadata() -> None:
    registry = build_registry()
    registry._register("custom.read", "Custom read.", lambda data: data)

    tool = next(tool for tool in registry.list() if tool["name"] == "custom.read")

    assert tool["permissionScope"] == "tool:custom.read:read"
    assert tool["timeoutMs"] == 1000
    assert tool["retryCount"] == 0
    assert tool["sideEffect"] == "read"
