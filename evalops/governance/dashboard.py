"""eval_dashboard — 本地 eval governance dashboard 生成器。

这是服务端可视化治理后台的本地静态版。它不启动 Web 服务,而是把已有
eval report / history / dataset 聚合成:
  - JSON summary,便于机器读取。
  - 单文件 HTML,便于本地查看和分享。

调用关系图:
  CLI: python -m agentic_core.eval_dashboard --report ... --history ... --dataset ...
    ├─▶ eval_history.summarize_history
    ├─▶ eval_sampling.build_review_queue
    ├─▶ eval_review.review_agreement
    ├─▶ eval_judge_registry.validate_dataset_rubrics
    └─▶ EvalDashboard.to_html / to_dict
"""

from __future__ import annotations

import argparse
import html
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evalops.history import read_eval_history, summarize_history, summarize_report
from evalops.judge_registry import validate_dataset_rubrics
from evalops.review import load_dataset, review_agreement
from evalops.sampling import build_review_queue
from agentic_core.memory.store import now_iso


DASHBOARD_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class EvalDashboard:
    """本地 dashboard 聚合结果。"""

    generated_at: str
    report_summary: dict[str, Any] | None
    history_summary: dict[str, Any] | None
    review_queue_summary: dict[str, Any] | None
    agreement_summary: dict[str, Any] | None
    rubric_validation: dict[str, Any] | None
    inputs: dict[str, str | None]
    schema_version: int = DASHBOARD_SCHEMA_VERSION
    dashboard_type: str = "agentic_eval_governance_dashboard"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "type": self.dashboard_type,
            "generatedAt": self.generated_at,
            "inputs": dict(self.inputs),
            "reportSummary": self.report_summary,
            "historySummary": self.history_summary,
            "reviewQueueSummary": self.review_queue_summary,
            "agreementSummary": self.agreement_summary,
            "rubricValidation": self.rubric_validation,
        }

    def to_html(self) -> str:
        data = self.to_dict()
        return "\n".join(
            [
                "<!doctype html>",
                '<html lang="zh-CN">',
                "<head>",
                '<meta charset="utf-8">',
                "<title>Agentic Eval Governance Dashboard</title>",
                "<style>",
                "body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;margin:32px;line-height:1.5;color:#1f2937}",
                "h1{font-size:28px;margin-bottom:4px} h2{font-size:18px;margin-top:28px;border-bottom:1px solid #d1d5db;padding-bottom:6px}",
                ".grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin:16px 0}",
                ".metric{border:1px solid #d1d5db;border-radius:6px;padding:12px;background:#f9fafb}",
                ".label{font-size:12px;color:#6b7280}.value{font-size:22px;font-weight:650}",
                "pre{background:#111827;color:#f9fafb;padding:14px;border-radius:6px;overflow:auto}",
                "table{border-collapse:collapse;width:100%;margin-top:10px}td,th{border:1px solid #d1d5db;padding:8px;text-align:left}",
                "</style>",
                "</head>",
                "<body>",
                "<h1>Agentic Eval Governance Dashboard</h1>",
                f"<p>Generated at {html.escape(self.generated_at)}</p>",
                self._report_section(),
                self._history_section(),
                self._review_section(),
                "<h2>Raw Summary</h2>",
                f"<pre>{html.escape(json.dumps(data, ensure_ascii=False, indent=2))}</pre>",
                "</body>",
                "</html>",
            ]
        )

    def _report_section(self) -> str:
        summary = self.report_summary or {}
        if not summary:
            return "<h2>Eval Report</h2><p>No report provided.</p>"
        return _metric_grid(
            "Eval Report",
            {
                "Gate": "PASS" if summary.get("passedGate") else "FAIL",
                "Cases": f"{summary.get('passed', 0)}/{summary.get('total', 0)}",
                "Case Pass Rate": summary.get("casePassRate", 0.0),
                "Tool Success Rate": summary.get("toolSuccessRate", 0.0),
            },
        )

    def _history_section(self) -> str:
        summary = self.history_summary or {}
        if not summary:
            return "<h2>Eval History</h2><p>No history provided.</p>"
        return _metric_grid(
            "Eval History",
            {
                "Records": summary.get("totalRecords", 0),
                "Gate Pass Rate": summary.get("gatePassRate", 0.0),
                "Regressions": len(summary.get("regressions", []) if isinstance(summary.get("regressions"), list) else []),
            },
        )

    def _review_section(self) -> str:
        queue = self.review_queue_summary or {}
        agreement = self.agreement_summary or {}
        validation = self.rubric_validation or {}
        return (
            _metric_grid(
                "Review And Governance",
                {
                    "Queue Items": queue.get("totalItems", 0),
                    "Review Conflicts": agreement.get("casesWithConflicts", 0),
                    "Rubrics Valid": "YES" if validation.get("valid", True) else "NO",
                    "Invalid Rubrics": validation.get("invalidCount", 0),
                },
            )
        )


def build_dashboard(
    report_path: str | Path | None = None,
    history_path: str | Path | None = None,
    dataset_path: str | Path | None = None,
) -> EvalDashboard:
    """聚合已有 eval 产物,构造 dashboard。"""

    report_summary = _report_summary(report_path)
    history_summary = _history_summary(history_path)
    dataset = load_dataset(dataset_path) if dataset_path else None
    review_queue_summary = None
    agreement_summary = None
    rubric_validation = None
    if dataset is not None:
        review_queue_summary = build_review_queue(dataset).to_dict()["summary"]
        agreement_summary = review_agreement(dataset)["summary"]
        rubric_validation = validate_dataset_rubrics(dataset)
    return EvalDashboard(
        generated_at=now_iso(),
        report_summary=report_summary,
        history_summary=history_summary,
        review_queue_summary=review_queue_summary,
        agreement_summary=agreement_summary,
        rubric_validation=rubric_validation,
        inputs={
            "report": str(report_path) if report_path else None,
            "history": str(history_path) if history_path else None,
            "dataset": str(dataset_path) if dataset_path else None,
        },
    )


def write_dashboard(
    dashboard: EvalDashboard,
    output_path: str | Path,
    json_output_path: str | Path | None = None,
) -> None:
    """写出 HTML dashboard 和可选 JSON summary。"""

    html_path = Path(output_path)
    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(dashboard.to_html(), encoding="utf-8")
    if json_output_path is not None:
        json_path = Path(json_output_path)
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(
            json.dumps(dashboard.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


def _report_summary(report_path: str | Path | None) -> dict[str, Any] | None:
    if report_path is None:
        return None
    data = json.loads(Path(report_path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("eval report must be a JSON object")
    return summarize_report(data)


def _history_summary(history_path: str | Path | None) -> dict[str, Any] | None:
    if history_path is None:
        return None
    return summarize_history(read_eval_history(history_path))


def _metric_grid(title: str, metrics: dict[str, Any]) -> str:
    cards = []
    for label, value in metrics.items():
        cards.append(
            "<div class=\"metric\">"
            f"<div class=\"label\">{html.escape(str(label))}</div>"
            f"<div class=\"value\">{html.escape(str(value))}</div>"
            "</div>"
        )
    return f"<h2>{html.escape(title)}</h2><div class=\"grid\">{''.join(cards)}</div>"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a local eval governance dashboard")
    parser.add_argument("--report", help="eval_harness --json 输出文件")
    parser.add_argument("--history", help="eval_history JSONL 文件")
    parser.add_argument("--dataset", help="eval dataset/golden JSON 文件")
    parser.add_argument("--output", required=True, help="输出 HTML 文件")
    parser.add_argument("--json-output", help="同时输出 JSON summary")
    args = parser.parse_args(argv)

    dashboard = build_dashboard(
        report_path=args.report,
        history_path=args.history,
        dataset_path=args.dataset,
    )
    write_dashboard(dashboard, args.output, json_output_path=args.json_output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
