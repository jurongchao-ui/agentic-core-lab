from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from agentic_core.memory.store import MemoryStore
from agentic_core.memory.lifecycle import (
    MemoryLifecyclePolicy,
    main,
    validate_memory_lifecycle_policy_file,
)
from agentic_core.runtime.schemas import MemoryRecord


def test_lifecycle_policy_separates_semantic_key_from_conflict_key() -> None:
    policy = MemoryLifecyclePolicy()
    memory = MemoryRecord(
        id="memory_1",
        memory_type="preference",
        text="用户偏好: 每次 30 分钟",
        reason="test",
        scores={},
        created_at="2026-07-04T00:00:00+00:00",
    )

    assert policy.semantic_key(memory.memory_type, memory.text) is None
    assert policy.conflict_key(memory) == "preference:study_session_duration"


def test_lifecycle_policy_generates_shared_tech_stack_key() -> None:
    policy = MemoryLifecyclePolicy()
    memory = MemoryRecord(
        id="memory_1",
        memory_type="user_profile",
        text="用户技术栈: Node.js、React、Codex",
        reason="test",
        scores={},
        created_at="2026-07-04T00:00:00+00:00",
    )

    assert policy.semantic_key(memory.memory_type, memory.text) == "user_profile:tech_stack"
    assert policy.conflict_key(memory) == "user_profile:tech_stack"


def test_lifecycle_policy_controls_importance_expiry_and_retention_key() -> None:
    policy = MemoryLifecyclePolicy(task_memory_ttl_days=7)
    memory = MemoryRecord(
        id="memory_1",
        memory_type="task_state",
        text="任务状态: 正在实现 Memory Lifecycle",
        reason="test",
        scores={"task_continuity": 5},
        created_at="2026-07-04T00:00:00+00:00",
        updated_at="2026-07-04T00:00:00+00:00",
        importance=policy.memory_importance("task_state", {"task_continuity": 5}),
        access_count=2,
    )
    memory.expires_at = policy.default_expiry(memory.memory_type, memory.created_at)

    assert memory.importance > 0
    assert memory.expires_at == "2026-07-11T00:00:00+00:00"
    assert not policy.is_expired(memory, datetime(2026, 7, 10, tzinfo=timezone.utc))
    assert policy.is_expired(memory, datetime(2026, 7, 12, tzinfo=timezone.utc))
    assert policy.retention_sort_key(memory) == (memory.importance, 2, 1783123200.0)


def test_memory_store_uses_injected_lifecycle_policy_for_task_expiry() -> None:
    memory = MemoryStore(lifecycle_policy=MemoryLifecyclePolicy(task_memory_ttl_days=3))

    record = memory.add_long_term_memory(
        "task_state",
        "任务状态: 自定义过期策略",
        "test",
        {"task_continuity": 5},
    )

    assert record.expires_at is not None
    assert record.expires_at.startswith("20")
    assert record.expires_at[:10] > record.created_at[:10]


def test_lifecycle_policy_loads_partial_json_config(tmp_path) -> None:
    path = tmp_path / "memory-lifecycle-policy.json"
    path.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "taskMemoryTtlDays": 5,
                "typeImportanceBoosts": {"user_profile": 80},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    policy = MemoryLifecyclePolicy.from_file(path)

    assert policy.task_memory_ttl_days == 5
    assert policy.type_importance_boosts["user_profile"] == 80
    assert policy.type_importance_boosts["preference"] == 25
    assert policy.to_dict()["taskMemoryTtlDays"] == 5


def test_lifecycle_policy_can_override_positive_score_keys() -> None:
    policy = MemoryLifecyclePolicy.from_dict(
        {
            "schemaVersion": 1,
            "positiveScoreKeys": ["future_relevance"],
            "typeImportanceBoosts": {"preference": 0},
        }
    )

    assert policy.memory_importance("preference", {"future_relevance": 5, "stability": 5}) == 40


def test_lifecycle_policy_rejects_invalid_config() -> None:
    with pytest.raises(ValueError, match="schemaVersion"):
        MemoryLifecyclePolicy.from_dict({"schemaVersion": 2})

    with pytest.raises(ValueError, match="taskMemoryTtlDays"):
        MemoryLifecyclePolicy.from_dict({"taskMemoryTtlDays": -1})

    with pytest.raises(ValueError, match="positiveScoreKeys"):
        MemoryLifecyclePolicy.from_dict({"positiveScoreKeys": "future_relevance"})

    with pytest.raises(ValueError, match="typeImportanceBoosts"):
        MemoryLifecyclePolicy.from_dict({"typeImportanceBoosts": {"preference": -1}})


def test_lifecycle_policy_cli_show_default_json(capsys) -> None:
    code = main(["show", "--json"])
    output = json.loads(capsys.readouterr().out)

    assert code == 0
    assert output["type"] == "agentic_memory_lifecycle_policy"
    assert output["path"] is None
    assert output["policy"]["taskMemoryTtlDays"] == 30


def test_lifecycle_policy_cli_show_file_text(tmp_path, capsys) -> None:
    path = tmp_path / "memory-lifecycle-policy.json"
    path.write_text(
        json.dumps({"schemaVersion": 1, "taskMemoryTtlDays": 9}, ensure_ascii=False),
        encoding="utf-8",
    )

    code = main(["show", "--path", str(path)])
    output = capsys.readouterr().out

    assert code == 0
    assert "Memory Lifecycle Policy" in output
    assert "taskMemoryTtlDays: 9" in output


def test_lifecycle_policy_validate_file_reports_success(tmp_path, capsys) -> None:
    path = tmp_path / "memory-lifecycle-policy.json"
    path.write_text(
        json.dumps({"schemaVersion": 1, "typeImportanceBoosts": {"preference": 40}}, ensure_ascii=False),
        encoding="utf-8",
    )

    code = main(["validate", "--path", str(path), "--json"])
    output = json.loads(capsys.readouterr().out)

    assert code == 0
    assert output["type"] == "agentic_memory_lifecycle_policy_validation"
    assert output["valid"] is True
    assert output["errors"] == []
    assert output["policy"]["typeImportanceBoosts"]["preference"] == 40


def test_lifecycle_policy_validate_file_reports_errors(tmp_path, capsys) -> None:
    path = tmp_path / "memory-lifecycle-policy.json"
    path.write_text(json.dumps({"schemaVersion": 1, "taskMemoryTtlDays": -1}), encoding="utf-8")

    report = validate_memory_lifecycle_policy_file(path)
    code = main(["validate", "--path", str(path), "--json"])
    output = json.loads(capsys.readouterr().out)

    assert report["valid"] is False
    assert code == 1
    assert output["valid"] is False
    assert "taskMemoryTtlDays" in output["errors"][0]
