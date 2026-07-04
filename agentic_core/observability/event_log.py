"""event_log — 持久事件的读取 / 过滤 / 时间线展示(排障工具,只读)。

功能:
  - read_jsonl_events 读取 events.jsonl(默认连同轮转备份 .1/.2…),坏行降级成
    invalid_jsonl_line 事件而非崩溃 —— reader 只检查/展示,不修改历史事件。
  - read_sqlite_events 读取 SQLite events 表里的 event_json,用于本地结构化查询。
  - filter_events_by_run_id / list_run_ids / format_timeline 按 runId 重建人读时间线。
  - _summarize_event 针对每种事件类型给一行摘要(safety/memory/planner/tool/response…)。
  - 命令行入口: python -m agentic_core.event_log [--backend jsonl|sqlite] [--run-id … | --json]。

调用关系图:
  CLI: python -m agentic_core.event_log
      └─▶ read_jsonl_events(path) / read_sqlite_events(path)
          └─▶ filter_events_by_run_id / list_run_ids ─▶ format_timeline
  数据来源: event_writer.JsonlEventWriter 写出的 data/events.jsonl(+ 轮转备份),
          或 SQLiteEventWriter 写出的 data/events.db。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any


DEFAULT_EVENT_LOG_PATH = Path("data/events.jsonl")
DEFAULT_SQLITE_EVENT_LOG_PATH = Path("data/events.db")


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


def read_sqlite_events(path: str | Path = DEFAULT_SQLITE_EVENT_LOG_PATH) -> list[dict[str, Any]]:
    """读取 SQLite events 表里的完整事件 JSON。

    reader 返回普通 dict,和 JSONL reader 保持同一输出形态。
    如果数据库或 events 表不存在,返回空列表,方便在本地排障时直接运行命令。
    """

    event_path = Path(path)
    if not event_path.exists():
        return []

    try:
        connection = sqlite3.connect(event_path)
        try:
            rows = connection.execute(
                """
                SELECT event_json
                FROM events
                ORDER BY created_at ASC, rowid ASC
                """
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.Error:
        return []

    events: list[dict[str, Any]] = []
    for (event_json,) in rows:
        try:
            event = json.loads(str(event_json))
        except json.JSONDecodeError:
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
    parser = argparse.ArgumentParser(description="查看 Agentic Core 持久事件时间线")
    parser.add_argument(
        "--backend",
        choices=["jsonl", "sqlite"],
        default="jsonl",
        help="事件日志后端",
    )
    parser.add_argument("--path", help="事件文件路径; jsonl 默认 data/events.jsonl, sqlite 默认 data/events.db")
    parser.add_argument("--run-id", help="只查看某个 runId")
    parser.add_argument("--current-only", action="store_true", help="只读取当前 JSONL 文件,不读取轮转备份")
    parser.add_argument("--json", action="store_true", help="输出 JSON 数组,不格式化")
    args = parser.parse_args()

    if args.backend == "sqlite":
        events = read_sqlite_events(args.path or DEFAULT_SQLITE_EVENT_LOG_PATH)
    else:
        events = read_jsonl_events(args.path or DEFAULT_EVENT_LOG_PATH, include_backups=not args.current_only)
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
