"""eval_sampling — 从 eval dataset 生成本地复核队列。

生产里的 golden dataset 不会一次性全量人工看完,通常会先抽样:
  - 未审核的 case 优先。
  - 缺 judge label 的 case 优先。
  - 安全拒绝、工具失败、记忆写入这类高风险链路优先。

本模块提供标准库学习版:
  - 输入 eval_dataset/eval_review 使用的 dataset JSON。
  - 输出 agentic_eval_review_queue。
  - 每个 queue item 带 priority / reasons / 原始 case。

调用关系图:
  CLI: python -m agentic_core.eval_sampling --input data/eval-dataset.json
    └─▶ load_dataset ─▶ build_review_queue ─▶ JSON review queue
  下游: 人工查看 queue 后,继续用 eval_review apply 写入审核和 judge label。
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .eval_review import load_dataset
from .memory import now_iso


QUEUE_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class ReviewQueueItem:
    """一条需要人工复核的 eval case。"""

    queue_id: str
    case_name: str
    goal: str
    priority: int
    reasons: list[str]
    source_run_id: str
    review_required: bool
    case: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "queueId": self.queue_id,
            "caseName": self.case_name,
            "goal": self.goal,
            "priority": self.priority,
            "reasons": list(self.reasons),
            "sourceRunId": self.source_run_id,
            "reviewRequired": self.review_required,
            "case": dict(self.case),
        }


@dataclass(frozen=True)
class ReviewQueue:
    """可保存为 JSON 的复核队列。"""

    items: list[ReviewQueueItem]
    source: dict[str, Any]
    sample_policy: dict[str, Any]
    generated_at: str
    schema_version: int = QUEUE_SCHEMA_VERSION
    queue_type: str = "agentic_eval_review_queue"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "type": self.queue_type,
            "generatedAt": self.generated_at,
            "source": dict(self.source),
            "samplePolicy": dict(self.sample_policy),
            "summary": _summary(self.items),
            "items": [item.to_dict() for item in self.items],
        }


def build_review_queue(
    dataset: dict[str, Any],
    limit: int | None = None,
    include_ready: bool = False,
    require_reasons: list[str] | None = None,
) -> ReviewQueue:
    """从 dataset 构造复核队列。

    include_ready=False 时,没有任何复核原因的 case 不进入队列。
    require_reasons 可用于只看某类问题,例如 ["needs_judge_label"]。
    """

    required_reason_set = set(require_reasons or [])
    items: list[ReviewQueueItem] = []
    for case in _cases(dataset):
        reasons = _sampling_reasons(case)
        if required_reason_set and not required_reason_set.intersection(reasons):
            continue
        if not include_ready and not reasons:
            continue
        items.append(_queue_item(case, reasons))

    items.sort(key=lambda item: (-item.priority, item.case_name))
    if limit is not None:
        items = items[: max(0, limit)]
    return ReviewQueue(
        items=items,
        source={
            "kind": "eval_dataset",
            "datasetType": dataset.get("type", "agentic_eval_dataset"),
            "datasetGeneratedAt": dataset.get("generatedAt"),
        },
        sample_policy={
            "limit": limit,
            "includeReady": include_ready,
            "requireReasons": list(require_reasons or []),
            "strategy": "priority_desc_name_asc",
        },
        generated_at=now_iso(),
    )


def format_review_queue(queue: ReviewQueue) -> str:
    """格式化成人可读的复核队列。"""

    data = queue.to_dict()
    summary = data["summary"]
    lines = [
        "Agentic Eval Review Queue",
        f"Items: {summary['totalItems']}",
    ]
    reason_counts = summary.get("reasonCounts")
    if isinstance(reason_counts, dict) and reason_counts:
        lines.append("Reasons:")
        for reason, count in sorted(reason_counts.items()):
            lines.append(f"- {reason}: {count}")
    lines.append("Queue:")
    for item in queue.items:
        lines.append(
            f"- p{item.priority} {item.case_name}: {', '.join(item.reasons) or 'ready'}"
        )
    return "\n".join(lines)


def _queue_item(case: dict[str, Any], reasons: list[str]) -> ReviewQueueItem:
    case_name = str(case.get("name", "case"))
    return ReviewQueueItem(
        queue_id=f"review_{case_name}",
        case_name=case_name,
        goal=str(case.get("goal", "")),
        priority=_priority(reasons),
        reasons=reasons,
        source_run_id=str(case.get("sourceRunId", "")),
        review_required=bool(case.get("reviewRequired", False)),
        case=dict(case),
    )


def _sampling_reasons(case: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    if bool(case.get("reviewRequired", False)):
        reasons.append("review_required")
    if not _string_list(case.get("expectedAnswerContains")):
        reasons.append("needs_answer_label")
    if case.get("expectedJudgeScore") is None or case.get("expectedJudgePassed") is None:
        reasons.append("needs_judge_label")
    if bool(case.get("expectedSafetyRefusal", False)):
        reasons.append("safety_case")
    if _int_value(case.get("expectedToolFailures")) > 0:
        reasons.append("tool_failure_case")
    if _int_value(case.get("expectedMemorySaves")) > 0:
        reasons.append("memory_write_case")
    return reasons


def _priority(reasons: list[str]) -> int:
    weights = {
        "review_required": 50,
        "needs_judge_label": 25,
        "safety_case": 25,
        "tool_failure_case": 20,
        "needs_answer_label": 15,
        "memory_write_case": 10,
    }
    return sum(weights.get(reason, 0) for reason in reasons)


def _summary(items: list[ReviewQueueItem]) -> dict[str, Any]:
    reason_counts: dict[str, int] = {}
    for item in items:
        for reason in item.reasons:
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
    return {
        "totalItems": len(items),
        "maxPriority": max((item.priority for item in items), default=0),
        "reasonCounts": reason_counts,
    }


def _cases(dataset: dict[str, Any]) -> list[dict[str, Any]]:
    cases = dataset.get("cases")
    if not isinstance(cases, list):
        return []
    return [case for case in cases if isinstance(case, dict)]


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _int_value(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build an eval review queue from a dataset")
    parser.add_argument("--input", required=True, help="输入 eval dataset JSON")
    parser.add_argument("--output", help="输出 review queue JSON;不传则打印")
    parser.add_argument("--limit", type=int, help="最多输出多少条 queue item")
    parser.add_argument("--include-ready", action="store_true", help="包含没有复核原因的 ready case")
    parser.add_argument("--reason", action="append", default=[], help="只保留包含指定 reason 的 case,可重复")
    parser.add_argument("--json", action="store_true", help="打印 JSON 而不是文本摘要")
    args = parser.parse_args(argv)

    dataset = load_dataset(args.input)
    queue = build_review_queue(
        dataset,
        limit=args.limit,
        include_ready=args.include_ready,
        require_reasons=args.reason,
    )
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(queue.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return 0
    if args.json:
        print(json.dumps(queue.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(format_review_queue(queue))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
