"""eval_replay — 从 Event Log 生成本地 replay inspection bundle。

这里的 replay 是“复盘/inspection”,不是确定性重放。它不会重新执行 LLM 或工具,
而是把某个 runId 的事件链路整理成稳定 JSON,供 debug、评测回放和人工复核使用。

调用关系图:
  CLI: python -m agentic_core.eval_replay --run-id ...
    └─▶ read_events_for_backend ─▶ filter_events_by_run_id
          └─▶ build_replay_bundle ─▶ JSON / 文本摘要
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from typing import Any

from .eval_dataset import read_events_for_backend
from .event_log import filter_events_by_run_id, format_timeline
from .memory import now_iso


REPLAY_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class ReplayBundle:
    """一次 run 的结构化复盘包。"""

    run_id: str
    goal: str
    status: str
    answer: str
    event_counts: dict[str, int]
    tool_calls: list[dict[str, Any]]
    safety: dict[str, Any] | None
    memory_decision: dict[str, Any] | None
    response_decision: dict[str, Any] | None
    timeline: str
    events: list[dict[str, Any]]
    generated_at: str
    source: dict[str, Any]
    schema_version: int = REPLAY_SCHEMA_VERSION
    bundle_type: str = "agentic_eval_replay_bundle"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "type": self.bundle_type,
            "generatedAt": self.generated_at,
            "source": dict(self.source),
            "runId": self.run_id,
            "goal": self.goal,
            "status": self.status,
            "answer": self.answer,
            "eventCounts": dict(self.event_counts),
            "toolCalls": [dict(item) for item in self.tool_calls],
            "safety": dict(self.safety) if self.safety is not None else None,
            "memoryDecision": dict(self.memory_decision) if self.memory_decision is not None else None,
            "responseDecision": dict(self.response_decision) if self.response_decision is not None else None,
            "timeline": self.timeline,
            "events": [dict(event) for event in self.events],
        }


def build_replay_bundle(
    events: list[dict[str, Any]],
    run_id: str,
    source: dict[str, Any] | None = None,
) -> ReplayBundle:
    """从事件列表构造某个 runId 的复盘包。"""

    run_events = filter_events_by_run_id(events, run_id)
    if not run_events:
        raise ValueError(f"runId not found: {run_id}")
    return ReplayBundle(
        run_id=run_id,
        goal=_goal(run_events),
        status=_status(run_events),
        answer=_answer(run_events),
        event_counts=_event_counts(run_events),
        tool_calls=_tool_calls(run_events),
        safety=_latest_payload_dict(run_events, "safety_decision", "safety"),
        memory_decision=_latest_payload_dict(run_events, "memory_decision", "decision"),
        response_decision=_latest_payload_dict(run_events, "response_decision", "responseDecision"),
        timeline=format_timeline(run_events),
        events=run_events,
        generated_at=now_iso(),
        source=source or {"kind": "event_log"},
    )


def format_replay_bundle(bundle: ReplayBundle) -> str:
    """格式化成人可读复盘摘要。"""

    lines = [
        "Agentic Replay Bundle",
        f"Run: {bundle.run_id}",
        f"Goal: {bundle.goal}",
        f"Status: {bundle.status}",
        f"Answer: {bundle.answer}",
        "Event counts:",
    ]
    for event_type, count in sorted(bundle.event_counts.items()):
        lines.append(f"- {event_type}: {count}")
    if bundle.tool_calls:
        lines.append("Tool calls:")
        for tool in bundle.tool_calls:
            lines.append(
                f"- step={tool.get('step')} tool={tool.get('toolName')} ok={tool.get('ok')}"
            )
    lines.append("Timeline:")
    lines.append(bundle.timeline)
    return "\n".join(lines)


def _goal(events: list[dict[str, Any]]) -> str:
    for event in events:
        if event.get("type") != "run_started":
            continue
        payload = _payload(event)
        return str(payload.get("goal", ""))
    return ""


def _status(events: list[dict[str, Any]]) -> str:
    for event in reversed(events):
        if event.get("type") == "run_completed":
            return str(_payload(event).get("status", "completed"))
        if event.get("type") == "run_failed":
            return "failed"
    return "unknown"


def _answer(events: list[dict[str, Any]]) -> str:
    for event in reversed(events):
        payload = _payload(event)
        if event.get("type") == "run_completed":
            return str(payload.get("answer", ""))
        if event.get("type") == "run_failed":
            return str(payload.get("error", ""))
    return ""


def _event_counts(events: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for event in events:
        event_type = str(event.get("type", "event"))
        counts[event_type] = counts.get(event_type, 0) + 1
    return counts


def _tool_calls(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for event in events:
        if event.get("type") != "tool_observation":
            continue
        payload = _payload(event)
        raw_action = payload.get("action")
        raw_observation = payload.get("observation")
        action: dict[str, Any] = raw_action if isinstance(raw_action, dict) else {}
        observation: dict[str, Any] = raw_observation if isinstance(raw_observation, dict) else {}
        calls.append(
            {
                "step": payload.get("step"),
                "toolName": action.get("toolName"),
                "ok": observation.get("ok"),
                "error": observation.get("error"),
            }
        )
    return calls


def _latest_payload_dict(events: list[dict[str, Any]], event_type: str, key: str) -> dict[str, Any] | None:
    for event in reversed(events):
        if event.get("type") != event_type:
            continue
        value = _payload(event).get(key)
        if isinstance(value, dict):
            return value
    return None


def _payload(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload")
    return payload if isinstance(payload, dict) else {}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a replay inspection bundle from event log")
    parser.add_argument("--backend", choices=["jsonl", "sqlite"], default="jsonl", help="事件日志后端")
    parser.add_argument("--path", help="事件文件路径")
    parser.add_argument("--run-id", required=True, help="要复盘的 runId")
    parser.add_argument("--current-only", action="store_true", help="JSONL 模式只读取当前文件")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args(argv)

    events = read_events_for_backend(args.backend, args.path, current_only=args.current_only)
    bundle = build_replay_bundle(
        events,
        run_id=args.run_id,
        source={"kind": "event_log", "backend": args.backend, "path": args.path},
    )
    if args.json:
        print(json.dumps(bundle.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(format_replay_bundle(bundle))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
