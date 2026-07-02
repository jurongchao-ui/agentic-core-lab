from __future__ import annotations

import json

from agentic_core.event_log import (
    filter_events_by_run_id,
    format_timeline,
    list_run_ids,
    read_jsonl_events,
)


def test_read_jsonl_events_returns_empty_list_for_missing_file(tmp_path) -> None:
    assert read_jsonl_events(tmp_path / "missing.jsonl") == []


def test_read_jsonl_events_reads_valid_lines_and_marks_invalid_lines(tmp_path) -> None:
    path = tmp_path / "events.jsonl"
    valid_event = {
        "id": "event_1",
        "type": "run_started",
        "runId": "run_1",
        "createdAt": "2026-07-02T00:00:00+00:00",
        "payload": {"goal": "hello"},
    }
    path.write_text(json.dumps(valid_event, ensure_ascii=False) + "\nnot-json\n", encoding="utf-8")

    events = read_jsonl_events(path)

    assert events[0]["id"] == "event_1"
    assert events[1]["type"] == "invalid_jsonl_line"
    assert events[1]["level"] == "error"
    assert events[1]["payload"]["file"] == "events.jsonl"


def test_read_jsonl_events_reads_rotated_backups_from_oldest_to_current(tmp_path) -> None:
    path = tmp_path / "events.jsonl"
    (tmp_path / "events.jsonl.2").write_text(
        json.dumps({"id": "event_1", "runId": "run_1", "type": "run_started"}) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "events.jsonl.1").write_text(
        json.dumps({"id": "event_2", "runId": "run_1", "type": "tool_observation"}) + "\n",
        encoding="utf-8",
    )
    path.write_text(
        json.dumps({"id": "event_3", "runId": "run_1", "type": "run_completed"}) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "events.jsonl.lock").write_text("", encoding="utf-8")

    events = read_jsonl_events(path)

    assert [event["id"] for event in events] == ["event_1", "event_2", "event_3"]


def test_read_jsonl_events_can_ignore_rotated_backups(tmp_path) -> None:
    path = tmp_path / "events.jsonl"
    (tmp_path / "events.jsonl.1").write_text(
        json.dumps({"id": "event_1", "runId": "run_1", "type": "run_started"}) + "\n",
        encoding="utf-8",
    )
    path.write_text(
        json.dumps({"id": "event_2", "runId": "run_1", "type": "run_completed"}) + "\n",
        encoding="utf-8",
    )

    events = read_jsonl_events(path, include_backups=False)

    assert [event["id"] for event in events] == ["event_2"]


def test_filter_events_by_run_id() -> None:
    events = [
        {"runId": "run_1", "type": "run_started"},
        {"runId": "run_2", "type": "run_started"},
        {"runId": "run_1", "type": "run_completed"},
    ]

    assert filter_events_by_run_id(events, "run_1") == [events[0], events[2]]


def test_list_run_ids_keeps_first_seen_order() -> None:
    events = [
        {"runId": "run_2"},
        {"runId": "run_1"},
        {"runId": "run_2"},
    ]

    assert list_run_ids(events) == ["run_2", "run_1"]


def test_format_timeline_summarizes_common_events() -> None:
    events = [
        {
            "type": "run_started",
            "runId": "run_1",
            "source": "agent",
            "level": "info",
            "createdAt": "2026-07-02T00:00:00+00:00",
            "payload": {"goal": "安排学习"},
        },
        {
            "type": "tool_observation",
            "runId": "run_1",
            "source": "tool",
            "level": "info",
            "createdAt": "2026-07-02T00:00:01+00:00",
            "payload": {
                "action": {"toolName": "calculator"},
                "observation": {"ok": True},
            },
        },
        {
            "type": "planner_action",
            "runId": "run_1",
            "source": "planner",
            "level": "info",
            "createdAt": "2026-07-02T00:00:02+00:00",
            "payload": {
                "step": 1,
                "action": {"type": "tool", "toolName": "calculator", "source": "rule"},
            },
        },
        {
            "type": "run_failed",
            "runId": "run_1",
            "source": "agent",
            "level": "error",
            "createdAt": "2026-07-02T00:00:03+00:00",
            "payload": {"errorType": "RuntimeError", "error": "boom"},
        },
        {
            "type": "safety_decision",
            "runId": "run_1",
            "source": "safety",
            "level": "info",
            "createdAt": "2026-07-02T00:00:04+00:00",
            "payload": {
                "safety": {
                    "refuse": True,
                    "category": "malware",
                    "riskLevel": "high",
                    "matchedRule": "malware.ransomware",
                }
            },
        },
        {
            "type": "planner_skipped",
            "runId": "run_1",
            "source": "planner",
            "level": "info",
            "createdAt": "2026-07-02T00:00:05+00:00",
            "payload": {"reason": "No tool intent detected"},
        },
    ]

    timeline = format_timeline(events)

    assert "agent.run_started" in timeline
    assert "goal=安排学习" in timeline
    assert "tool=calculator ok=True" in timeline
    assert "action=tool tool=calculator source=rule" in timeline
    assert "errorType=RuntimeError error=boom" in timeline
    assert "risk=high rule=malware.ransomware" in timeline
    assert "planner.planner_skipped" in timeline
    assert "No tool intent detected" in timeline
