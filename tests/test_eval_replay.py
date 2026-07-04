from __future__ import annotations

import json

import pytest

from agentic_core.eval_replay import build_replay_bundle, format_replay_bundle, main


def test_build_replay_bundle_extracts_run_summary() -> None:
    bundle = build_replay_bundle(sample_events(), run_id="run_1")
    data = bundle.to_dict()

    assert data["type"] == "agentic_eval_replay_bundle"
    assert data["runId"] == "run_1"
    assert data["goal"] == "帮我计算 128 * 7"
    assert data["status"] == "completed"
    assert data["answer"] == "计算结果是 896。"
    assert data["eventCounts"]["tool_observation"] == 1
    assert data["toolCalls"] == [
        {"step": 1, "toolName": "calculator", "ok": True, "error": None}
    ]
    assert data["safety"]["refuse"] is False
    assert data["memoryDecision"]["save"] is False
    assert data["responseDecision"]["tiers"] == ["tool_result_summary"]
    assert "agent.run_started" in data["timeline"]


def test_build_replay_bundle_raises_for_missing_run_id() -> None:
    with pytest.raises(ValueError, match="runId not found"):
        build_replay_bundle(sample_events(), run_id="missing")


def test_format_replay_bundle_contains_timeline_and_counts() -> None:
    text = format_replay_bundle(build_replay_bundle(sample_events(), run_id="run_1"))

    assert "Agentic Replay Bundle" in text
    assert "Run: run_1" in text
    assert "- tool_observation: 1" in text
    assert "step=1 tool=calculator ok=True" in text
    assert "Timeline:" in text


def test_eval_replay_cli_outputs_json(tmp_path, capsys) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text(
        "\n".join(json.dumps(event, ensure_ascii=False) for event in sample_events()) + "\n",
        encoding="utf-8",
    )

    code = main(["--path", str(path), "--run-id", "run_1", "--json"])

    data = json.loads(capsys.readouterr().out)
    assert code == 0
    assert data["runId"] == "run_1"
    assert data["eventCounts"]["run_completed"] == 1


def sample_events() -> list[dict]:
    return [
        {
            "id": "event_1",
            "type": "run_started",
            "runId": "run_1",
            "createdAt": "2026-07-04T00:00:00+00:00",
            "source": "agent",
            "level": "info",
            "payload": {"goal": "帮我计算 128 * 7"},
        },
        {
            "id": "event_2",
            "type": "safety_decision",
            "runId": "run_1",
            "createdAt": "2026-07-04T00:00:01+00:00",
            "source": "safety",
            "level": "info",
            "payload": {"safety": {"refuse": False}},
        },
        {
            "id": "event_3",
            "type": "memory_decision",
            "runId": "run_1",
            "createdAt": "2026-07-04T00:00:02+00:00",
            "source": "memory",
            "level": "info",
            "payload": {"decision": {"save": False}},
        },
        {
            "id": "event_4",
            "type": "tool_observation",
            "runId": "run_1",
            "createdAt": "2026-07-04T00:00:03+00:00",
            "source": "tool",
            "level": "info",
            "payload": {
                "step": 1,
                "action": {"toolName": "calculator"},
                "observation": {"ok": True},
            },
        },
        {
            "id": "event_5",
            "type": "response_decision",
            "runId": "run_1",
            "createdAt": "2026-07-04T00:00:04+00:00",
            "source": "response",
            "level": "info",
            "payload": {"responseDecision": {"tiers": ["tool_result_summary"]}},
        },
        {
            "id": "event_6",
            "type": "run_completed",
            "runId": "run_1",
            "createdAt": "2026-07-04T00:00:05+00:00",
            "source": "agent",
            "level": "info",
            "payload": {"status": "completed", "answer": "计算结果是 896。"},
        },
        {
            "id": "event_other",
            "type": "run_started",
            "runId": "run_2",
            "createdAt": "2026-07-04T00:00:06+00:00",
            "source": "agent",
            "level": "info",
            "payload": {"goal": "其他任务"},
        },
    ]
