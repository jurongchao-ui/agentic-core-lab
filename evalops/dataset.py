"""eval_dataset — 从 Event Log 生成 eval golden dataset 草稿。

真实生产里的 eval 不应该只靠手写的 8 条用例。更常见的做法是:

  1. 从线上/本地事件日志里抽取真实 run。
  2. 生成待审核的 golden dataset 草稿。
  3. 人工确认哪些断言稳定、哪些自然语言片段适合作为期望。
  4. 再交给 eval_harness 做跨版本回归。

本模块先做标准库学习版:
  - 读取 JSONL 或 SQLite event log。
  - 按 runId 聚合事件。
  - 抽取 goal/status/tools/response tiers/memory saves/tool failures/event counts。
  - 输出 JSON dataset,默认 reviewRequired=true。

调用关系图:
  CLI: python -m agentic_core.eval_dataset [--backend jsonl|sqlite ...]
    └─▶ read_events_for_backend(event log) ─▶ build_eval_dataset_from_events
          └─▶ build_eval_case_from_run_events(按 runId 聚合) ─▶ EvalDatasetCase(reviewRequired=true)
    └─▶ 输出 JSON dataset 草稿
  数据来源: event_writer 写的 JSONL/SQLite; 下游: eval_review 审核 ─▶ eval_harness --cases 回归。
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from agentic_core.observability.event_log import (
    DEFAULT_EVENT_LOG_PATH,
    DEFAULT_SQLITE_EVENT_LOG_PATH,
    filter_events_by_run_id,
    list_run_ids,
    read_jsonl_events,
    read_sqlite_events,
)
from evalops.judge import DEFAULT_JUDGE_RUBRIC_NAME, DEFAULT_JUDGE_RUBRIC_VERSION
from agentic_core.memory.store import now_iso


DATASET_SCHEMA_VERSION = 1


@dataclass
class EvalDatasetCase:
    """从某次历史 run 抽取出的 eval case 草稿。"""

    name: str
    goal: str
    source_run_id: str
    expected_status: str
    expected_tools: list[str] = field(default_factory=list)
    expected_answer_contains: list[str] = field(default_factory=list)
    expected_memory_saves: int | None = None
    expected_safety_refusal: bool | None = None
    expected_tool_failures: int | None = None
    expected_response_tiers: list[str] = field(default_factory=list)
    expected_event_counts: dict[str, int] = field(default_factory=dict)
    observed_answer: str = ""
    review_required: bool = True
    judge_rubric: str = DEFAULT_JUDGE_RUBRIC_NAME
    judge_rubric_version: str = DEFAULT_JUDGE_RUBRIC_VERSION
    expected_judge_score: int | None = None
    expected_judge_passed: bool | None = None
    judge_score_tolerance: int = 10
    judge_notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        return {
            "name": data["name"],
            "goal": data["goal"],
            "sourceRunId": data["source_run_id"],
            "observedAnswer": data["observed_answer"],
            "reviewRequired": data["review_required"],
            "expectedStatus": data["expected_status"],
            "expectedTools": data["expected_tools"],
            "expectedAnswerContains": data["expected_answer_contains"],
            "expectedMemorySaves": data["expected_memory_saves"],
            "expectedSafetyRefusal": data["expected_safety_refusal"],
            "expectedToolFailures": data["expected_tool_failures"],
            "expectedResponseTiers": data["expected_response_tiers"],
            "expectedEventCounts": data["expected_event_counts"],
            "judgeRubric": data["judge_rubric"],
            "judgeRubricVersion": data["judge_rubric_version"],
            "expectedJudgeScore": data["expected_judge_score"],
            "expectedJudgePassed": data["expected_judge_passed"],
            "judgeScoreTolerance": data["judge_score_tolerance"],
            "judgeNotes": data["judge_notes"],
        }


@dataclass
class EvalDataset:
    """可保存到 JSON 的 eval dataset。"""

    cases: list[EvalDatasetCase]
    source: dict[str, Any]
    generated_at: str
    schema_version: int = DATASET_SCHEMA_VERSION
    dataset_type: str = "agentic_eval_dataset"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "type": self.dataset_type,
            "generatedAt": self.generated_at,
            "source": dict(self.source),
            "cases": [case.to_dict() for case in self.cases],
        }


def build_eval_dataset_from_events(
    events: list[dict[str, Any]],
    source: dict[str, Any] | None = None,
    include_failed: bool = False,
) -> EvalDataset:
    """从事件列表构造 eval dataset 草稿。"""

    cases: list[EvalDatasetCase] = []
    for run_id in list_run_ids(events):
        run_events = filter_events_by_run_id(events, run_id)
        case = build_eval_case_from_run_events(run_id, run_events, include_failed=include_failed)
        if case is not None:
            cases.append(case)
    return EvalDataset(
        cases=cases,
        source=source or {"kind": "event_log"},
        generated_at=now_iso(),
    )


def build_eval_case_from_run_events(
    run_id: str,
    events: list[dict[str, Any]],
    include_failed: bool = False,
) -> EvalDatasetCase | None:
    """从单次 run 的事件中抽取一条 eval case。

    不完整 run 会返回 None。失败 run 默认跳过,除非 include_failed=True。
    """

    goal = _goal_from_events(events)
    if not goal:
        return None

    status, answer = _status_and_answer_from_events(events)
    if status is None:
        return None
    if status == "failed" and not include_failed:
        return None

    return EvalDatasetCase(
        name=_case_name(run_id, goal),
        goal=goal,
        source_run_id=run_id,
        observed_answer=answer,
        expected_status=status,
        expected_tools=_tool_names(events),
        expected_answer_contains=_answer_contains_candidates(answer),
        expected_memory_saves=_event_counts(events).get("memory_saved", 0),
        expected_safety_refusal=_event_counts(events).get("safety_refusal", 0) > 0,
        expected_tool_failures=_tool_failure_count(events),
        expected_response_tiers=_response_tiers(events),
        expected_event_counts=_stable_event_counts(events),
        review_required=True,
    )


def read_events_for_backend(backend: str, path: str | Path | None, current_only: bool = False) -> list[dict[str, Any]]:
    """读取指定后端的事件。"""

    if backend == "sqlite":
        return read_sqlite_events(path or DEFAULT_SQLITE_EVENT_LOG_PATH)
    return read_jsonl_events(path or DEFAULT_EVENT_LOG_PATH, include_backups=not current_only)


def _goal_from_events(events: list[dict[str, Any]]) -> str:
    for event in events:
        if event.get("type") != "run_started":
            continue
        payload = _payload(event)
        goal = payload.get("goal")
        if isinstance(goal, str) and goal.strip():
            return goal
    return ""


def _status_and_answer_from_events(events: list[dict[str, Any]]) -> tuple[str | None, str]:
    for event in reversed(events):
        if event.get("type") == "run_completed":
            payload = _payload(event)
            status = payload.get("status")
            answer = payload.get("answer", "")
            return str(status or "completed"), str(answer or "")
        if event.get("type") == "run_failed":
            payload = _payload(event)
            return "failed", str(payload.get("error") or "")
    return None, ""


def _tool_names(events: list[dict[str, Any]]) -> list[str]:
    tools: list[str] = []
    for event in events:
        if event.get("type") != "tool_observation":
            continue
        action = _payload(event).get("action") or {}
        if not isinstance(action, dict):
            continue
        tool_name = action.get("toolName")
        if isinstance(tool_name, str) and tool_name:
            tools.append(tool_name)
    return tools


def _tool_failure_count(events: list[dict[str, Any]]) -> int:
    failures = 0
    for event in events:
        if event.get("type") != "tool_observation":
            continue
        observation = _payload(event).get("observation") or {}
        if isinstance(observation, dict) and observation.get("ok") is False:
            failures += 1
    return failures


def _response_tiers(events: list[dict[str, Any]]) -> list[str]:
    for event in reversed(events):
        if event.get("type") != "response_decision":
            continue
        decision = _payload(event).get("responseDecision") or {}
        if not isinstance(decision, dict):
            continue
        tiers = decision.get("tiers")
        if isinstance(tiers, list):
            return [str(tier) for tier in tiers]
    return []


def _event_counts(events: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for event in events:
        event_type = str(event.get("type", "event"))
        counts[event_type] = counts.get(event_type, 0) + 1
    return counts


def _stable_event_counts(events: list[dict[str, Any]]) -> dict[str, int]:
    """只导出适合回归断言的事件类型计数。

    低层 writer warning、invalid line 这类排障事件不适合作为行为期望。
    """

    unstable = {"event_writer_warning", "invalid_jsonl_line"}
    return {
        event_type: count
        for event_type, count in _event_counts(events).items()
        if event_type not in unstable
    }


def _answer_contains_candidates(answer: str) -> list[str]:
    """从历史答案里提取少量稳定候选片段。

    自动生成的自然语言断言必须克制,所以只提取数字和几个稳定业务词。
    人工 review 后可以继续补充或删除。
    """

    candidates: list[str] = []
    for number in re.findall(r"\d+(?:\.\d+)?", answer):
        if number not in candidates:
            candidates.append(number)
    for keyword in ["学习笔记", "已记住", "无法帮助", "技术栈", "计算失败"]:
        if keyword in answer and keyword not in candidates:
            candidates.append(keyword)
    return candidates[:5]


def _case_name(run_id: str, goal: str) -> str:
    slug = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "_", goal).strip("_")
    slug = slug[:24] or "case"
    return f"event_log_{run_id}_{slug}"


def _payload(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload")
    return payload if isinstance(payload, dict) else {}


def main() -> int:
    parser = argparse.ArgumentParser(description="从 Agentic Core event log 生成 eval dataset 草稿")
    parser.add_argument("--backend", choices=["jsonl", "sqlite"], default="jsonl", help="事件日志后端")
    parser.add_argument("--path", help="事件文件路径; jsonl 默认 data/events.jsonl, sqlite 默认 data/events.db")
    parser.add_argument("--current-only", action="store_true", help="JSONL 模式只读取当前文件,不读取轮转备份")
    parser.add_argument("--include-failed", action="store_true", help="包含 failed run")
    parser.add_argument("--output", help="输出 dataset JSON 文件;不传则打印到 stdout")
    args = parser.parse_args()

    events = read_events_for_backend(args.backend, args.path, current_only=args.current_only)
    dataset = build_eval_dataset_from_events(
        events,
        source={
            "kind": "event_log",
            "backend": args.backend,
            "path": args.path or str(DEFAULT_SQLITE_EVENT_LOG_PATH if args.backend == "sqlite" else DEFAULT_EVENT_LOG_PATH),
        },
        include_failed=args.include_failed,
    )
    text = json.dumps(dataset.to_dict(), ensure_ascii=False, indent=2)
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
