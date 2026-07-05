from __future__ import annotations

from agentic_core.memory.store import MemoryStore
from agentic_core.policies.memory import RuleBasedMemoryPolicy
from agentic_core.tools.registry import ToolRegistry


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
    assert calculator["owner"] == "agentic-core"
    assert calculator["slaTier"] == "local"
    assert calculator["dataClassification"] == "internal"
    assert calculator["auditClassification"] == "standard"
    assert calculator["externalSideEffect"] is False
    assert calculator["lifecycleStatus"] == "active"
    assert calculator["introducedIn"] == "1.0"
    assert calculator["deprecatedIn"] is None
    assert calculator["replacedBy"] is None
    assert calculator["migrationNotes"] is None


def test_write_tools_are_marked_with_write_scope_and_sensitive_guard() -> None:
    tools = {tool["name"]: tool for tool in build_registry().list()}

    assert tools["note.add"]["sideEffect"] == "write"
    assert tools["note.add"]["permissionScope"] == "memory:note:write"
    assert tools["note.add"]["riskLevel"] == "medium"
    assert tools["note.add"]["guardSensitive"] is True
    assert tools["note.add"]["auditClassification"] == "sensitive"
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
    assert tool["owner"] == "agentic-core"
    assert tool["slaTier"] == "local"


def test_custom_tool_can_override_governance_metadata() -> None:
    registry = build_registry()
    registry._register(
        "billing.charge",
        "Charge a test invoice.",
        lambda data: data,
        {"amount": {"type": "number", "required": True}},
        side_effect="write",
        owner="billing-team",
        sla_tier="critical",
        data_classification="confidential",
        audit_classification="financial",
        external_side_effect=True,
    )

    tool = next(tool for tool in registry.list() if tool["name"] == "billing.charge")

    assert tool["permissionScope"] == "tool:billing.charge:write"
    assert tool["owner"] == "billing-team"
    assert tool["slaTier"] == "critical"
    assert tool["dataClassification"] == "confidential"
    assert tool["auditClassification"] == "financial"
    assert tool["externalSideEffect"] is True


def test_tool_catalog_exposes_all_tools_and_validates_defaults() -> None:
    registry = build_registry()

    catalog = registry.catalog()
    report = registry.validate_catalog()

    assert catalog["type"] == "agentic_tool_catalog"
    assert catalog["schemaVersion"] == 1
    assert len(catalog["tools"]) == len(registry._tools)
    assert report == {
        "schemaVersion": 1,
        "type": "agentic_tool_catalog_validation",
        "valid": True,
        "toolCount": len(registry._tools),
        "errors": [],
    }


def test_deprecated_tool_requires_migration_metadata() -> None:
    registry = build_registry()
    registry._register(
        "old.note.add",
        "Old note writer.",
        lambda data: data,
        {"text": {"type": "string", "required": True}},
        lifecycle_status="deprecated",
        replaced_by="note.add",
    )

    report = registry.validate_catalog()

    assert report["valid"] is False
    assert "old.note.add: deprecated tool must set migrationNotes" in report["errors"]
    assert "old.note.add: deprecated tool must set deprecatedIn" in report["errors"]


def test_removed_tool_is_kept_in_catalog_but_hidden_from_planner_and_execution() -> None:
    registry = build_registry()
    registry._register(
        "old.todo.add",
        "Removed todo writer.",
        lambda data: data,
        {"text": {"type": "string", "required": True}},
        lifecycle_status="removed",
        replaced_by="todo.add",
        migration_notes="Use todo.add with the same text input.",
        deprecated_in="1.1",
    )

    visible_names = {tool["name"] for tool in registry.list()}
    catalog_names = {tool["name"] for tool in registry.catalog()["tools"]}
    report = registry.validate_catalog()

    assert "old.todo.add" not in visible_names
    assert "old.todo.add" in catalog_names
    assert report["valid"] is True
    try:
        registry.execute("old.todo.add", {"text": "x"})
    except ValueError as exc:
        assert "removed tool" in str(exc)
    else:
        raise AssertionError("removed tool execution should fail")
