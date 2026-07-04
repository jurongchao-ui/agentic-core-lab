"""eval_diff — 对比两次 eval JSON 报告。

生产里的 eval 不只问“当前是否通过”,还要问:

  - 相比上一个版本,哪些 case 从 pass 变成 fail?
  - 哪些指标变差了,例如 tool_success_rate 降低、run_failed 增加?
  - gate 状态有没有从 PASS 变成 FAIL?

本模块读取 `python -m agentic_core.eval_harness --json` 的输出,生成结构化 diff。
它是标准库学习版,适合本地和 CI 使用。

调用关系图:
  CLI: python -m agentic_core.eval_diff base.json candidate.json [--fail-on-regression]
    └─▶ diff_eval_reports(base, candidate) ─▶ EvalReportDiff(case 翻转 / 指标退化 / gate 变化)
    └─▶ format_eval_diff ─▶ 打印; 有退化时非 0 退出码接 CI
  输入: eval_harness --json 产出的两份报告。
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


RATE_METRICS = {"case_pass_rate", "tool_success_rate"}
LOWER_IS_BETTER_METRICS = {"tool_failures", "planner_fallbacks", "safety_refusals", "run_failed"}


@dataclass
class NumericDiff:
    """一个数值字段的变化。"""

    name: str
    base: int | float
    candidate: int | float
    delta: int | float
    direction: str
    regression: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CaseDiff:
    """单条 eval case 的变化。"""

    name: str
    change: str
    base_passed: bool | None = None
    candidate_passed: bool | None = None
    base_failures: list[str] = field(default_factory=list)
    candidate_failures: list[str] = field(default_factory=list)

    @property
    def regression(self) -> bool:
        return self.change in {"pass_to_fail", "removed"}

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "change": self.change,
            "basePassed": self.base_passed,
            "candidatePassed": self.candidate_passed,
            "baseFailures": list(self.base_failures),
            "candidateFailures": list(self.candidate_failures),
            "regression": self.regression,
        }


@dataclass
class EvalReportDiff:
    """两份 eval report 的差异汇总。"""

    base_path: str
    candidate_path: str
    base_passed_gate: bool
    candidate_passed_gate: bool
    gate_regression: bool
    metric_diffs: list[NumericDiff]
    event_count_diffs: list[NumericDiff]
    case_diffs: list[CaseDiff]

    @property
    def has_regression(self) -> bool:
        return (
            self.gate_regression
            or any(diff.regression for diff in self.metric_diffs)
            or any(diff.regression for diff in self.case_diffs)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "basePath": self.base_path,
            "candidatePath": self.candidate_path,
            "basePassedGate": self.base_passed_gate,
            "candidatePassedGate": self.candidate_passed_gate,
            "gateRegression": self.gate_regression,
            "hasRegression": self.has_regression,
            "metricDiffs": [diff.to_dict() for diff in self.metric_diffs],
            "eventCountDiffs": [diff.to_dict() for diff in self.event_count_diffs],
            "caseDiffs": [diff.to_dict() for diff in self.case_diffs],
        }


def diff_eval_reports(base_path: str | Path, candidate_path: str | Path) -> EvalReportDiff:
    """读取两份 eval report JSON 并生成 diff。"""

    base = _read_report(base_path)
    candidate = _read_report(candidate_path)
    base_gate = bool(base.get("passedGate", False))
    candidate_gate = bool(candidate.get("passedGate", False))
    return EvalReportDiff(
        base_path=str(base_path),
        candidate_path=str(candidate_path),
        base_passed_gate=base_gate,
        candidate_passed_gate=candidate_gate,
        gate_regression=base_gate and not candidate_gate,
        metric_diffs=_numeric_diffs(
            _number_dict(base.get("metrics")),
            _number_dict(candidate.get("metrics")),
            metric_kind="metric",
        ),
        event_count_diffs=_numeric_diffs(
            _number_dict(base.get("eventCounts")),
            _number_dict(candidate.get("eventCounts")),
            metric_kind="event_count",
        ),
        case_diffs=_case_diffs(_cases_by_name(base), _cases_by_name(candidate)),
    )


def format_eval_diff(diff: EvalReportDiff) -> str:
    """格式化成人能读的 eval diff。"""

    lines = [
        "Agentic Eval Diff",
        f"Base: {diff.base_path}",
        f"Candidate: {diff.candidate_path}",
        f"Gate: {'PASS' if diff.base_passed_gate else 'FAIL'} -> {'PASS' if diff.candidate_passed_gate else 'FAIL'}",
        f"Regression: {'YES' if diff.has_regression else 'NO'}",
    ]
    if diff.metric_diffs:
        lines.append("Metric Changes:")
        for metric_diff in diff.metric_diffs:
            mark = "REGRESSION" if metric_diff.regression else metric_diff.direction
            lines.append(
                f"- {metric_diff.name}: {metric_diff.base} -> {metric_diff.candidate} "
                f"({metric_diff.delta:+g}) {mark}"
            )
    if diff.case_diffs:
        lines.append("Case Changes:")
        for case_diff in diff.case_diffs:
            mark = "REGRESSION" if case_diff.regression else case_diff.change
            lines.append(f"- {case_diff.name}: {case_diff.change} {mark}")
            for failure in case_diff.candidate_failures:
                lines.append(f"  - {failure}")
    if diff.event_count_diffs:
        lines.append("Event Count Changes:")
        for event_diff in diff.event_count_diffs:
            lines.append(f"- {event_diff.name}: {event_diff.base} -> {event_diff.candidate} ({event_diff.delta:+g})")
    return "\n".join(lines)


def _read_report(path: str | Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("eval report must be a JSON object")
    return data


def _numeric_diffs(
    base: dict[str, int | float],
    candidate: dict[str, int | float],
    metric_kind: str,
) -> list[NumericDiff]:
    diffs: list[NumericDiff] = []
    for name in sorted(set(base) | set(candidate)):
        base_value = base.get(name, 0)
        candidate_value = candidate.get(name, 0)
        if base_value == candidate_value:
            continue
        delta = candidate_value - base_value
        diffs.append(
            NumericDiff(
                name=name,
                base=base_value,
                candidate=candidate_value,
                delta=delta,
                direction=_numeric_direction(name, delta),
                regression=_numeric_regression(name, delta, metric_kind),
            )
        )
    return diffs


def _numeric_direction(name: str, delta: int | float) -> str:
    if delta == 0:
        return "unchanged"
    if name in RATE_METRICS:
        return "improved" if delta > 0 else "worse"
    if name in LOWER_IS_BETTER_METRICS:
        return "improved" if delta < 0 else "worse"
    return "increased" if delta > 0 else "decreased"


def _numeric_regression(name: str, delta: int | float, metric_kind: str) -> bool:
    if metric_kind == "event_count":
        return False
    if name in RATE_METRICS:
        return delta < 0
    if name in LOWER_IS_BETTER_METRICS:
        return delta > 0
    return False


def _case_diffs(base: dict[str, dict[str, Any]], candidate: dict[str, dict[str, Any]]) -> list[CaseDiff]:
    diffs: list[CaseDiff] = []
    for name in sorted(set(base) | set(candidate)):
        base_case = base.get(name)
        candidate_case = candidate.get(name)
        if base_case is None:
            diffs.append(
                CaseDiff(
                    name=name,
                    change="added",
                    base_passed=None,
                    candidate_passed=bool(candidate_case.get("passed")) if candidate_case else None,
                    candidate_failures=_string_list(candidate_case.get("failures") if candidate_case else None),
                )
            )
            continue
        if candidate_case is None:
            diffs.append(
                CaseDiff(
                    name=name,
                    change="removed",
                    base_passed=bool(base_case.get("passed")),
                    candidate_passed=None,
                    base_failures=_string_list(base_case.get("failures")),
                )
            )
            continue
        base_passed = bool(base_case.get("passed"))
        candidate_passed = bool(candidate_case.get("passed"))
        if base_passed and not candidate_passed:
            change = "pass_to_fail"
        elif not base_passed and candidate_passed:
            change = "fail_to_pass"
        elif _string_list(base_case.get("failures")) != _string_list(candidate_case.get("failures")):
            change = "failure_changed"
        else:
            continue
        diffs.append(
            CaseDiff(
                name=name,
                change=change,
                base_passed=base_passed,
                candidate_passed=candidate_passed,
                base_failures=_string_list(base_case.get("failures")),
                candidate_failures=_string_list(candidate_case.get("failures")),
            )
        )
    return diffs


def _cases_by_name(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    cases = report.get("cases")
    if not isinstance(cases, list):
        return {}
    output: dict[str, dict[str, Any]] = {}
    for item in cases:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if isinstance(name, str) and name:
            output[name] = item
    return output


def _number_dict(value: Any) -> dict[str, int | float]:
    if not isinstance(value, dict):
        return {}
    output: dict[str, int | float] = {}
    for key, item in value.items():
        if isinstance(item, bool):
            continue
        if isinstance(item, int | float):
            output[str(key)] = item
    return output


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare two Agentic Core eval JSON reports")
    parser.add_argument("--base", required=True, help="基线 eval report JSON")
    parser.add_argument("--candidate", required=True, help="候选 eval report JSON")
    parser.add_argument("--json", action="store_true", help="输出 JSON diff")
    parser.add_argument("--fail-on-regression", action="store_true", help="发现回归时返回非 0")
    args = parser.parse_args(argv)

    diff = diff_eval_reports(args.base, args.candidate)
    if args.json:
        print(json.dumps(diff.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(format_eval_diff(diff))
    return 1 if args.fail_on_regression and diff.has_regression else 0


if __name__ == "__main__":
    raise SystemExit(main())
