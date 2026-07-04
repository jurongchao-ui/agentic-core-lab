"""eval_history — eval report 的本地趋势存储。

eval_diff 能比较两份报告,但生产里还需要“历史”:

  - 每次 CI / 本地验收后把 eval report 追加保存。
  - 能列出最近几次 gate/metrics。
  - 能快速看最新一次相对上一次是否退化。

本模块使用 JSONL 作为学习版 append-only 历史库。每行一条历史记录,包含:
  - summary: 便于快速趋势展示。
  - report: 完整 eval_harness JSON 报告,方便后续复盘和重新 diff。

调用关系图:
  CLI: python -m agentic_core.eval_history (append | show)
    └─▶ append_eval_history(report) ─▶ data/eval-history.jsonl(每行一条)
    └─▶ read_eval_history ─▶ summarize_history ─▶ format_history_summary(趋势 / 退化)
  输入: eval_harness --json 报告; 相关: eval_diff(两份报告对比)。
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .eval_diff import LOWER_IS_BETTER_METRICS, RATE_METRICS
from .memory import now_iso


DEFAULT_EVAL_HISTORY_PATH = Path("data/eval-history.jsonl")
HISTORY_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class EvalHistoryRecord:
    """一次 eval report 的历史记录。"""

    recorded_at: str
    summary: dict[str, Any]
    report: dict[str, Any]
    label: str | None = None
    commit: str | None = None
    schema_version: int = HISTORY_SCHEMA_VERSION
    record_type: str = "agentic_eval_history_record"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "type": self.record_type,
            "recordedAt": self.recorded_at,
            "label": self.label,
            "commit": self.commit,
            "summary": dict(self.summary),
            "report": dict(self.report),
        }


def append_eval_history(
    report_path: str | Path,
    history_path: str | Path = DEFAULT_EVAL_HISTORY_PATH,
    label: str | None = None,
    commit: str | None = None,
) -> EvalHistoryRecord:
    """把一份 eval report JSON 追加写入 history JSONL。"""

    report = _read_report(report_path)
    record = EvalHistoryRecord(
        recorded_at=now_iso(),
        label=label,
        commit=commit,
        summary=summarize_report(report),
        report=report,
    )
    path = Path(history_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(record.to_dict(), ensure_ascii=False, sort_keys=True) + "\n")
    return record


def read_eval_history(history_path: str | Path = DEFAULT_EVAL_HISTORY_PATH) -> list[dict[str, Any]]:
    """读取 history JSONL。坏行会作为 invalid_history_line 记录返回。"""

    path = Path(history_path)
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as error:
            records.append(
                {
                    "schemaVersion": HISTORY_SCHEMA_VERSION,
                    "type": "invalid_history_line",
                    "recordedAt": "",
                    "summary": {
                        "lineNumber": line_number,
                        "error": str(error),
                    },
                    "report": {},
                }
            )
            continue
        if isinstance(item, dict):
            records.append(item)
    return records


def summarize_report(report: dict[str, Any]) -> dict[str, Any]:
    """抽取 eval report 的趋势摘要。"""

    metrics = _dict_value(report, "metrics")
    event_counts = _dict_value(report, "eventCounts")
    return {
        "total": int(report.get("total", 0)),
        "passed": int(report.get("passed", 0)),
        "failed": int(report.get("failed", 0)),
        "passedGate": bool(report.get("passedGate", False)),
        "casePassRate": _number(metrics.get("case_pass_rate"), default=0.0),
        "toolSuccessRate": _number(metrics.get("tool_success_rate"), default=0.0),
        "toolFailures": int(_number(metrics.get("tool_failures"), default=0)),
        "plannerFallbacks": int(_number(metrics.get("planner_fallbacks"), default=0)),
        "runFailed": int(_number(metrics.get("run_failed"), default=0)),
        "metrics": dict(metrics),
        "eventCounts": dict(event_counts),
        "gateFailures": _string_list(report.get("gateFailures")),
    }


def summarize_history(records: list[dict[str, Any]], limit: int = 10) -> dict[str, Any]:
    """汇总历史趋势。"""

    valid_records = [record for record in records if record.get("type") == "agentic_eval_history_record"]
    recent = valid_records[-limit:] if limit > 0 else valid_records
    latest = valid_records[-1] if valid_records else None
    previous = valid_records[-2] if len(valid_records) >= 2 else None
    return {
        "schemaVersion": HISTORY_SCHEMA_VERSION,
        "type": "agentic_eval_history_summary",
        "totalRecords": len(valid_records),
        "gatePassRate": _gate_pass_rate(valid_records),
        "latest": _record_head(latest),
        "previous": _record_head(previous),
        "metricDeltas": _metric_deltas(previous, latest),
        "regressions": _history_regressions(previous, latest),
        "recent": [_record_head(record) for record in recent],
    }


def format_history_summary(summary: dict[str, Any]) -> str:
    """格式化成人可读趋势摘要。"""

    lines = [
        "Agentic Eval History",
        f"Records: {summary.get('totalRecords', 0)}",
        f"Gate pass rate: {summary.get('gatePassRate', 0.0)}",
    ]
    latest = summary.get("latest")
    if isinstance(latest, dict):
        lines.append(
            "Latest: "
            f"{latest.get('recordedAt', '')} "
            f"gate={'PASS' if latest.get('passedGate') else 'FAIL'} "
            f"passed={latest.get('passed')}/{latest.get('total')}"
        )
    regressions = summary.get("regressions")
    if isinstance(regressions, list) and regressions:
        lines.append("Regressions:")
        for regression in regressions:
            lines.append(f"- {regression}")
    metric_deltas = summary.get("metricDeltas")
    if isinstance(metric_deltas, list) and metric_deltas:
        lines.append("Metric deltas:")
        for delta in metric_deltas:
            if isinstance(delta, dict):
                lines.append(
                    f"- {delta.get('name')}: {delta.get('previous')} -> {delta.get('latest')} "
                    f"({delta.get('delta'):+g})"
                )
    recent = summary.get("recent")
    if isinstance(recent, list) and recent:
        lines.append("Recent:")
        for item in recent:
            if isinstance(item, dict):
                lines.append(
                    f"- {item.get('recordedAt', '')} "
                    f"{item.get('label') or ''} "
                    f"gate={'PASS' if item.get('passedGate') else 'FAIL'} "
                    f"passed={item.get('passed')}/{item.get('total')}"
                )
    return "\n".join(lines)


def _read_report(path: str | Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("eval report must be a JSON object")
    return data


def _record_head(record: dict[str, Any] | None) -> dict[str, Any] | None:
    if record is None:
        return None
    summary = _dict_value(record, "summary")
    return {
        "recordedAt": record.get("recordedAt", ""),
        "label": record.get("label"),
        "commit": record.get("commit"),
        "total": summary.get("total", 0),
        "passed": summary.get("passed", 0),
        "failed": summary.get("failed", 0),
        "passedGate": summary.get("passedGate", False),
        "casePassRate": summary.get("casePassRate", 0.0),
        "toolSuccessRate": summary.get("toolSuccessRate", 0.0),
        "toolFailures": summary.get("toolFailures", 0),
        "runFailed": summary.get("runFailed", 0),
    }


def _metric_deltas(previous: dict[str, Any] | None, latest: dict[str, Any] | None) -> list[dict[str, Any]]:
    if previous is None or latest is None:
        return []
    previous_metrics = _metrics(previous)
    latest_metrics = _metrics(latest)
    deltas: list[dict[str, Any]] = []
    for name in sorted(set(previous_metrics) | set(latest_metrics)):
        previous_value = previous_metrics.get(name, 0)
        latest_value = latest_metrics.get(name, 0)
        if previous_value == latest_value:
            continue
        delta = latest_value - previous_value
        deltas.append(
            {
                "name": name,
                "previous": previous_value,
                "latest": latest_value,
                "delta": delta,
                "regression": _metric_regression(name, delta),
            }
        )
    return deltas


def _history_regressions(previous: dict[str, Any] | None, latest: dict[str, Any] | None) -> list[str]:
    if previous is None or latest is None:
        return []
    regressions: list[str] = []
    previous_summary = _summary(previous)
    latest_summary = _summary(latest)
    if previous_summary.get("passedGate") is True and latest_summary.get("passedGate") is False:
        regressions.append("gate changed from PASS to FAIL")
    for delta in _metric_deltas(previous, latest):
        if delta["regression"]:
            regressions.append(
                f"{delta['name']} regressed: {delta['previous']} -> {delta['latest']}"
            )
    return regressions


def _metrics(record: dict[str, Any]) -> dict[str, int | float]:
    summary = _summary(record)
    metrics = summary.get("metrics")
    if not isinstance(metrics, dict):
        return {}
    output: dict[str, int | float] = {}
    for key, value in metrics.items():
        if isinstance(value, bool):
            continue
        if isinstance(value, int | float):
            output[str(key)] = value
    return output


def _summary(record: dict[str, Any]) -> dict[str, Any]:
    return _dict_value(record, "summary")


def _dict_value(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    return value if isinstance(value, dict) else {}


def _metric_regression(name: str, delta: int | float) -> bool:
    if name in RATE_METRICS:
        return delta < 0
    if name in LOWER_IS_BETTER_METRICS:
        return delta > 0
    return False


def _gate_pass_rate(records: list[dict[str, Any]]) -> float:
    if not records:
        return 1.0
    passed = 0
    for record in records:
        if _summary(record).get("passedGate") is True:
            passed += 1
    return passed / len(records)


def _number(value: Any, default: int | float) -> int | float:
    if isinstance(value, bool):
        return default
    if isinstance(value, int | float):
        return value
    return default


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Store and inspect Agentic Core eval report history")
    subparsers = parser.add_subparsers(dest="command", required=True)

    append_parser = subparsers.add_parser("append", help="追加一份 eval report 到 history")
    append_parser.add_argument("--report", required=True, help="eval_harness --json 输出文件")
    append_parser.add_argument("--history", default=str(DEFAULT_EVAL_HISTORY_PATH), help="history JSONL 路径")
    append_parser.add_argument("--label", help="记录标签,例如 branch/build id")
    append_parser.add_argument("--commit", help="代码提交 SHA")

    list_parser = subparsers.add_parser("list", help="列出 eval history 摘要")
    list_parser.add_argument("--history", default=str(DEFAULT_EVAL_HISTORY_PATH), help="history JSONL 路径")
    list_parser.add_argument("--limit", type=int, default=10, help="显示最近 N 条")
    list_parser.add_argument("--json", action="store_true", help="输出 JSON summary")

    args = parser.parse_args(argv)
    if args.command == "append":
        record = append_eval_history(
            report_path=args.report,
            history_path=args.history,
            label=args.label,
            commit=args.commit,
        )
        print(json.dumps(record.to_dict(), ensure_ascii=False, indent=2))
        return 0

    records = read_eval_history(args.history)
    summary = summarize_history(records, limit=args.limit)
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(format_history_summary(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
