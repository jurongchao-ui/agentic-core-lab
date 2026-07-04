from __future__ import annotations

from agentic_core.agent import Agent
from agentic_core.event_payloads import (
    RunStartedPayload,
    validate_event_payload,
)
from agentic_core.memory import MemoryStore
from agentic_core.memory_policy import RuleBasedMemoryPolicy
from agentic_core.planner import RuleBasedPlanner
from agentic_core.tools import ToolRegistry


def test_typed_event_payload_to_dict_uses_public_json_names() -> None:
    payload = RunStartedPayload(goal="hello", identity={"userId": "u1"})

    data = payload.to_dict()

    assert data == {"goal": "hello", "identity": {"userId": "u1"}}


def test_validate_event_payload_marks_missing_required_fields() -> None:
    validation = validate_event_payload("run_started", {"goal": "hello"})

    assert validation.valid is False
    assert validation.missing_required == ("identity",)
    assert validation.errors == ("missing required payload field: identity",)


def test_memory_store_records_payload_schema_validation_result() -> None:
    memory = MemoryStore()

    event = memory.record_event(event_type="run_started", run_id="run_1", payload={"goal": "hello"})

    assert event.payload_schema_valid is False
    assert event.payload_schema_errors == ["missing required payload field: identity"]
    assert event.to_dict()["payloadSchema"] == {
        "version": 1,
        "valid": False,
        "errors": ["missing required payload field: identity"],
    }


def test_agent_mainline_events_have_valid_payload_schema() -> None:
    memory = MemoryStore()
    policy = RuleBasedMemoryPolicy()
    agent = Agent(
        planner=RuleBasedPlanner(),
        tools=ToolRegistry(memory, policy),
        memory=memory,
        memory_policy=policy,
    )

    result = agent.run_typed("帮我计算 128 * 7, 然后记录成学习笔记")

    assert result.events
    assert all(event.payload_schema_valid for event in result.events)
    assert all(event.to_dict()["payloadSchema"]["valid"] for event in result.events)
