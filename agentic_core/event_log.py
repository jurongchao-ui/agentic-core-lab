from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


DEFAULT_EVENT_LOG_PATH = Path("data/events.jsonl")


def read_jsonl_events(
    path: str | Path = DEFAULT_EVENT_LOG_PATH,
    include_backups: bool = True,
) -> list[dict[str, Any]]:
    """读取 JSONL 事件文件。

    JSONL 是“一行一条 JSON”。这里返回普通 dict,因为它可能来自旧 schema、
    新 schema、甚至未来数据库导出的事件。reader 的职责是检查和展示,不是修改事件。

    默认会一起读取轮转备份文件:
        events.jsonl.2 -> events.jsonl.1 -> events.jsonl

    这样刚发生轮转时,排障工具仍能看到较完整的 run 时间线。
    """

    event_path = Path(path)
    event_paths = _event_log_paths(event_path, include_backups=include_backups)
    if not event_paths:
        return []

    events: list[dict[str, Any]] = []
    for current_path in event_paths:
        events.extend(_read_single_jsonl_file(current_path))
    return events


def _event_log_paths(path: Path, include_backups: bool) -> list[Path]:
    """返回要读取的 JSONL 文件列表。

    数字越大的备份越老,所以读取顺序是 `.N` 到 `.1`,最后读当前文件。
    `.lock` 不是事件文件,不会被纳入。
    """

    paths: list[tuple[int, Path]] = []
    if include_backups:
        for candidate in path.parent.glob(f"{path.name}.*"):
            suffix = candidate.name.removeprefix(f"{path.name}.")
            if suffix.isdigit():
                paths.append((int(suffix), candidate))
    paths.sort(key=lambda item: item[0], reverse=True)
    ordered_paths = [candidate for _, candidate in paths]
    if path.exists():
        ordered_paths.append(path)
    return ordered_paths


def _read_single_jsonl_file(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as error:
            events.append(
                {
                    "id": f"invalid_line_{path.name}_{line_number}",
                    "type": "invalid_jsonl_line",
                    "runId": "unknown",
                    "createdAt": "",
                    "source": "event_log",
                    "level": "error",
                    "payload": {
                        "file": path.name,
                        "lineNumber": line_number,
                        "error": str(error),
                    },
                }
            )
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def filter_events_by_run_id(events: list[dict[str, Any]], run_id: str) -> list[dict[str, Any]]:
    """按 runId 过滤事件。"""

    return [event for event in events if event.get("runId") == run_id]


def list_run_ids(events: list[dict[str, Any]]) -> list[str]:
    """按首次出现顺序列出 runId。"""

    run_ids: list[str] = []
    seen: set[str] = set()
    for event in events:
        run_id = str(event.get("runId", "unknown"))
        if run_id not in seen:
            seen.add(run_id)
            run_ids.append(run_id)
    return run_ids


def format_timeline(events: list[dict[str, Any]]) -> str:
    """把一组事件格式化成人能读的时间线。"""

    if not events:
        return "没有找到事件。"

    lines: list[str] = []
    for event in events:
        event_type = event.get("type", "event")
        run_id = event.get("runId", "unknown")
        source = event.get("source", "unknown")
        level = event.get("level", "info")
        created_at = event.get("createdAt", "")
        redacted = " redacted" if event.get("redacted") else ""
        summary = _summarize_event(event)
        lines.append(f"{created_at} [{level}] {run_id} {source}.{event_type}{redacted} - {summary}")
    return "\n".join(lines)


def _summarize_event(event: dict[str, Any]) -> str:
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return ""

    event_type = event.get("type")
    if event_type == "run_started":
        return f"goal={payload.get('goal', '')}"
    if event_type == "safety_decision":
        safety = payload.get("safety") or {}
        if isinstance(safety, dict):
            return (
                f"refuse={safety.get('refuse')} category={safety.get('category')} "
                f"risk={safety.get('riskLevel')} rule={safety.get('matchedRule')}"
            )
    if event_type == "safety_refusal":
        safety = payload.get("safety") or {}
        if isinstance(safety, dict):
            return (
                f"category={safety.get('category')} rule={safety.get('matchedRule')} "
                f"answer={payload.get('answer', '')}"
            )
    if event_type == "memory_decision":
        decision = payload.get("decision") or {}
        if isinstance(decision, dict):
            return f"save={decision.get('save')} type={decision.get('memory_type')}"
    if event_type == "memory_saved":
        memory = payload.get("savedMemory") or {}
        if isinstance(memory, dict):
            return f"memory={memory.get('text', '')}"
    if event_type in {"planner_action", "planner_fallback", "tool_started"}:
        action = payload.get("action") or {}
        if isinstance(action, dict):
            action_type = action.get("type")
            tool_name = action.get("toolName")
            source = action.get("source")
            return f"step={payload.get('step')} action={action_type} tool={tool_name} source={source}"
    if event_type == "planner_skipped":
        return f"reason={payload.get('reason', '')}"
    if event_type == "tool_observation":
        action = payload.get("action") or {}
        observation = payload.get("observation") or {}
        if isinstance(action, dict) and isinstance(observation, dict):
            return f"tool={action.get('toolName')} ok={observation.get('ok')}"
    if event_type == "response_decision":
        return f"answer={payload.get('answer', '')}"
    if event_type == "run_completed":
        return f"status={payload.get('status')} answer={payload.get('answer', '')}"
    if event_type == "run_failed":
        return f"errorType={payload.get('errorType')} error={payload.get('error')}"
    if event_type == "event_writer_warning":
        return f"writer={payload.get('writer')} error={payload.get('error')}"
    if event_type == "invalid_jsonl_line":
        return f"line={payload.get('lineNumber')} error={payload.get('error')}"
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="查看 Agentic Core JSONL 事件时间线")
    parser.add_argument("--path", default=str(DEFAULT_EVENT_LOG_PATH), help="JSONL 事件文件路径")
    parser.add_argument("--run-id", help="只查看某个 runId")
    parser.add_argument("--current-only", action="store_true", help="只读取当前 JSONL 文件,不读取轮转备份")
    parser.add_argument("--json", action="store_true", help="输出 JSON 数组,不格式化")
    args = parser.parse_args()

    events = read_jsonl_events(args.path, include_backups=not args.current_only)
    if args.run_id:
        events = filter_events_by_run_id(events, args.run_id)

    if args.json:
        print(json.dumps(events, ensure_ascii=False, indent=2))
        return 0

    if not args.run_id:
        run_ids = list_run_ids(events)
        print("Runs:")
        for run_id in run_ids:
            count = len(filter_events_by_run_id(events, run_id))
            print(f"- {run_id}: {count} events")
        if run_ids:
            print("\n使用 --run-id 查看某次运行的时间线。")
        return 0

    print(format_timeline(events))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
