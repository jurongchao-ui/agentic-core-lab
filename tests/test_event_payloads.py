from __future__ import annotations

from agentic_core.runtime.agent import Agent
from agentic_core.observability.event_payloads import (
    RunStartedPayload,
    migrate_event_payload,
    validate_event_payload,
)
from agentic_core.memory.store import MemoryStore
from agentic_core.policies.memory import RuleBasedMemoryPolicy
from agentic_core.policies.planner import RuleBasedPlanner
from agentic_core.tools.registry import ToolRegistry


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
        "version": 2,
        "valid": False,
        "errors": ["missing required payload field: identity"],
        "migrationsApplied": [],
    }


def test_migrate_event_payload_adds_unknown_identity_for_old_run_started() -> None:
    migration = migrate_event_payload(
        "run_started",
        {"goal": "hello"},
        source_schema_version=1,
    )

    assert migration.payload["goal"] == "hello"
    assert migration.payload["identity"] == {
        "userId": "unknown",
        "tenantId": "unknown",
        "roles": [],
        "permissionScopes": None,
    }
    assert migration.source_schema_version == 1
    assert migration.target_schema_version == 2
    assert migration.migrations_applied == ("run_started.v1.add_unknown_identity",)
    assert validate_event_payload("run_started", migration.payload).valid is True


def test_memory_store_migrates_legacy_flat_tool_observation_event() -> None:
    memory = MemoryStore()

    event = memory.record_event(
        {
            "runId": "run_legacy",
            "type": "tool_observation",
            "step": 1,
            "toolName": "calculator",
            "ok": True,
            "output": {"result": 896},
        }
    )

    assert event.payload_schema_valid is True
    assert event.payload == {
        "step": 1,
        "action": {
            "type": "tool",
            "toolName": "calculator",
            "input": {},
            "reason": "migrated legacy tool event",
            "source": "legacy",
        },
        "observation": {
            "ok": True,
            "output": {"result": 896},
            "error": None,
            "elapsed_ms": 0,
            "metadata": {},
        },
    }
    assert event.payload_schema_migrations == ["legacy_flat_tool_observation_to_v1"]


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
