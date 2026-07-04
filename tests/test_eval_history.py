from __future__ import annotations

import json

from evalops.history import (
    append_eval_history,
    format_history_summary,
    main,
    read_eval_history,
    summarize_history,
)


def test_append_and_read_eval_history(tmp_path) -> None:
    report_path = tmp_path / "report.json"
    history_path = tmp_path / "history.jsonl"
    report_path.write_text(json.dumps(report(passed_gate=True)), encoding="utf-8")

    record = append_eval_history(report_path, history_path, label="local", commit="abc123")
    records = read_eval_history(history_path)

    assert record.summary["passedGate"] is True
    assert record.summary["toolSuccessRate"] == 1.0
    assert len(records) == 1
    assert records[0]["label"] == "local"
    assert records[0]["commit"] == "abc123"
    assert records[0]["summary"]["passed"] == 1
    assert records[0]["report"]["passedGate"] is True


def test_summarize_history_detects_latest_regression(tmp_path) -> None:
    history_path = tmp_path / "history.jsonl"
    base_report = tmp_path / "base.json"
    latest_report = tmp_path / "latest.json"
    base_report.write_text(json.dumps(report(passed_gate=True, tool_success_rate=1.0)), encoding="utf-8")
    latest_report.write_text(
        json.dumps(report(passed_gate=False, tool_success_rate=0.5, tool_failures=1)),
        encoding="utf-8",
    )
    append_eval_history(base_report, history_path, label="base")
    append_eval_history(latest_report, history_path, label="latest")

    summary = summarize_history(read_eval_history(history_path))

    assert summary["totalRecords"] == 2
    assert summary["gatePassRate"] == 0.5
    assert summary["latest"]["label"] == "latest"
    assert "gate changed from PASS to FAIL" in summary["regressions"]
    assert "tool_success_rate regressed: 1.0 -> 0.5" in summary["regressions"]
    assert "tool_failures regressed: 0 -> 1" in summary["regressions"]


def test_format_history_summary_is_human_readable(tmp_path) -> None:
    history_path = tmp_path / "history.jsonl"
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(report(passed_gate=True)), encoding="utf-8")
    append_eval_history(report_path, history_path, label="local")

    text = format_history_summary(summarize_history(read_eval_history(history_path)))

    assert "Agentic Eval History" in text
    assert "Records: 1" in text
    assert "gate=PASS" in text
    assert "local" in text


def test_eval_history_cli_append_and_list(tmp_path, capsys) -> None:
    report_path = tmp_path / "report.json"
    history_path = tmp_path / "history.jsonl"
    report_path.write_text(json.dumps(report(passed_gate=True)), encoding="utf-8")

    append_code = main(
        [
            "append",
            "--report",
            str(report_path),
            "--history",
            str(history_path),
            "--label",
            "cli",
        ]
    )
    list_code = main(["list", "--history", str(history_path)])

    output = capsys.readouterr().out
    assert append_code == 0
    assert list_code == 0
    assert "Agentic Eval History" in output
    assert "cli" in output


def report(
    passed_gate: bool,
    tool_success_rate: float = 1.0,
    tool_failures: int = 0,
) -> dict:
    return {
        "total": 1,
        "passed": 1 if passed_gate else 0,
        "failed": 0 if passed_gate else 1,
        "passedGate": passed_gate,
        "metrics": {
            "case_pass_rate": 1.0 if passed_gate else 0.0,
            "tool_calls": 2,
            "tool_failures": tool_failures,
            "tool_success_rate": tool_success_rate,
            "planner_fallbacks": 0,
            "run_failed": 0,
        },
        "eventCounts": {"run_started": 1},
        "gateFailures": [] if passed_gate else ["gate failed"],
        "cases": [],
    }
