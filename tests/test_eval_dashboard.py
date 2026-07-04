from __future__ import annotations

import json

from agentic_core.eval_dashboard import build_dashboard, main, write_dashboard


def test_build_dashboard_aggregates_report_history_and_dataset(tmp_path) -> None:
    report_path = tmp_path / "report.json"
    history_path = tmp_path / "history.jsonl"
    dataset_path = tmp_path / "dataset.json"
    report_path.write_text(json.dumps(sample_report(passed_gate=True)), encoding="utf-8")
    history_path.write_text(
        json.dumps(
            {
                "type": "agentic_eval_history_record",
                "recordedAt": "2026-07-04T00:00:00+00:00",
                "summary": {"passedGate": True, "metrics": {"case_pass_rate": 1.0}},
                "report": sample_report(passed_gate=True),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    dataset_path.write_text(json.dumps(sample_dataset()), encoding="utf-8")

    dashboard = build_dashboard(report_path, history_path, dataset_path)
    data = dashboard.to_dict()

    assert data["type"] == "agentic_eval_governance_dashboard"
    assert data["reportSummary"]["passedGate"] is True
    assert data["historySummary"]["totalRecords"] == 1
    assert data["reviewQueueSummary"]["totalItems"] == 1
    assert data["agreementSummary"]["casesWithConflicts"] == 0
    assert data["rubricValidation"]["valid"] is True


def test_dashboard_html_contains_core_sections(tmp_path) -> None:
    dashboard = build_dashboard(
        report_path=_write_json(tmp_path / "report.json", sample_report(passed_gate=True)),
        dataset_path=_write_json(tmp_path / "dataset.json", sample_dataset()),
    )

    html = dashboard.to_html()

    assert "Agentic Eval Governance Dashboard" in html
    assert "Eval Report" in html
    assert "Review And Governance" in html
    assert "Raw Summary" in html


def test_write_dashboard_writes_html_and_json(tmp_path) -> None:
    dashboard = build_dashboard(report_path=_write_json(tmp_path / "report.json", sample_report(True)))
    html_path = tmp_path / "dashboard.html"
    json_path = tmp_path / "dashboard.json"

    write_dashboard(dashboard, html_path, json_output_path=json_path)

    assert "Agentic Eval Governance Dashboard" in html_path.read_text(encoding="utf-8")
    assert json.loads(json_path.read_text(encoding="utf-8"))["type"] == "agentic_eval_governance_dashboard"


def test_eval_dashboard_cli_writes_outputs(tmp_path) -> None:
    report_path = _write_json(tmp_path / "report.json", sample_report(True))
    dataset_path = _write_json(tmp_path / "dataset.json", sample_dataset())
    html_path = tmp_path / "dashboard.html"
    json_path = tmp_path / "dashboard.json"

    code = main(
        [
            "--report",
            str(report_path),
            "--dataset",
            str(dataset_path),
            "--output",
            str(html_path),
            "--json-output",
            str(json_path),
        ]
    )

    assert code == 0
    assert html_path.exists()
    assert json.loads(json_path.read_text(encoding="utf-8"))["reviewQueueSummary"]["totalItems"] == 1


def sample_report(passed_gate: bool) -> dict:
    return {
        "total": 1,
        "passed": 1 if passed_gate else 0,
        "failed": 0 if passed_gate else 1,
        "passedGate": passed_gate,
        "metrics": {"case_pass_rate": 1.0 if passed_gate else 0.0, "tool_success_rate": 1.0},
        "eventCounts": {"run_completed": 1},
        "gateFailures": [] if passed_gate else ["gate failed"],
        "cases": [],
    }


def sample_dataset() -> dict:
    return {
        "schemaVersion": 1,
        "type": "agentic_eval_dataset",
        "generatedAt": "2026-07-04T00:00:00+00:00",
        "cases": [
            {
                "name": "case_needs_review",
                "goal": "帮我计算",
                "reviewRequired": True,
                "expectedAnswerContains": [],
                "judgeRubric": "agentic_core_default",
                "judgeRubricVersion": "v1",
            }
        ],
        "reviewDecisions": [
            {
                "caseName": "case_needs_review",
                "status": "approved",
                "reviewer": "a",
                "judgeLabels": {"expectedJudgeScore": 100, "expectedJudgePassed": True},
            }
        ],
    }


def _write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path
