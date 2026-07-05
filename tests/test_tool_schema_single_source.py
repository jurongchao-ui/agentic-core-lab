from __future__ import annotations

import pytest

from agentic_core.memory.store import MemoryStore
from agentic_core.policies.memory import RuleBasedMemoryPolicy
from agentic_core.policies.planner import describe_input_field, validate_tool_input
from agentic_core.tools.registry import ToolRegistry, input_schema_to_json_schema


def build_registry() -> ToolRegistry:
    return ToolRegistry(MemoryStore(), RuleBasedMemoryPolicy())


def test_registry_exposes_schema() -> None:
    tools = build_registry().list()
    assert all("inputSchema" in tool for tool in tools)
    assert all("inputJsonSchema" in tool for tool in tools)
    calculator = next(tool for tool in tools if tool["name"] == "calculator")
    assert calculator["inputSchema"]["expression"]["required"] is True
    assert calculator["inputJsonSchema"]["type"] == "object"
    assert calculator["inputJsonSchema"]["required"] == ["expression"]
    assert calculator["inputJsonSchema"]["properties"]["expression"]["type"] == "string"
    assert calculator["inputJsonSchema"]["additionalProperties"] is False


def test_validate_from_registry() -> None:
    available_tools = build_registry().list()
    with pytest.raises(ValueError):
        validate_tool_input("calculator", {}, available_tools)
    validate_tool_input("calculator", {"expression": "1 + 1"}, available_tools)


def test_new_tool_propagates() -> None:
    """单一真相源: 只在 registry 注册新工具,校验自动跟随,无需改 planner。"""
    registry = build_registry()
    registry._register(
        "greet.hi",
        "Say hi.",
        lambda input_data: input_data,
        {"name": {"type": "string", "required": True}},
    )
    available_tools = registry.list()
    with pytest.raises(ValueError):
        validate_tool_input("greet.hi", {}, available_tools)
    validate_tool_input("greet.hi", {"name": "x"}, available_tools)
    greet = next(tool for tool in available_tools if tool["name"] == "greet.hi")
    assert greet["inputJsonSchema"]["required"] == ["name"]


def test_input_schema_to_json_schema_preserves_optional_fields() -> None:
    schema = input_schema_to_json_schema(
        {
            "topic": {"type": "string", "required": True, "description": "Study topic"},
            "max_minutes": {"type": "integer", "required": False},
        }
    )

    assert schema == {
        "type": "object",
        "properties": {
            "topic": {"type": "string", "description": "Study topic"},
            "max_minutes": {"type": "integer"},
        },
        "additionalProperties": False,
        "required": ["topic"],
    }


def test_describe_input_field() -> None:
    assert describe_input_field({"type": "string", "required": True}) == "string, required"
    assert describe_input_field({"type": "string"}) == "string, optional"
