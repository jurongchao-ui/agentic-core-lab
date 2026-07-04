from __future__ import annotations

import json

from evalops.diff import diff_eval_reports, format_eval_diff, main


def test_diff_eval_reports_detects_metric_and_case_regressions(tmp_path) -> None:
    base_path = tmp_path / "base.json"
    candidate_path = tmp_path / "candidate.json"
    base_path.write_text(json.dumps(report(passed_gate=True, case_passed=True)), encoding="utf-8")
    candidate_path.write_text(
        json.dumps(
            report(
                passed_gate=False,
                case_passed=False,
                failures=["answer missing text: 896"],
                tool_success_rate=0.5,
                tool_failures=1,
            )
        ),
        encoding="utf-8",
    )

    diff = diff_eval_reports(base_path, candidate_path)
    data = diff.to_dict()

    assert diff.has_regression is True
    assert data["gateRegression"] is True
    assert any(item["name"] == "tool_success_rate" and item["regression"] for item in data["metricDiffs"])
    assert any(item["name"] == "tool_failures" and item["regression"] for item in data["metricDiffs"])
    assert data["caseDiffs"][0]["change"] == "pass_to_fail"
    assert data["caseDiffs"][0]["regression"] is True


def test_diff_eval_reports_marks_fail_to_pass_as_improvement(tmp_path) -> None:
    base_path = tmp_path / "base.json"
    candidate_path = tmp_path / "candidate.json"
    base_path.write_text(json.dumps(report(passed_gate=False, case_passed=False, failures=["bad"])), encoding="utf-8")
    candidate_path.write_text(json.dumps(report(passed_gate=True, case_passed=True)), encoding="utf-8")

    diff = diff_eval_reports(base_path, candidate_path)

    assert diff.has_regression is False
    assert diff.case_diffs[0].change == "fail_to_pass"


def test_format_eval_diff_is_human_readable(tmp_path) -> None:
    base_path = tmp_path / "base.json"
    candidate_path = tmp_path / "candidate.json"
    base_path.write_text(json.dumps(report(passed_gate=True, case_passed=True)), encoding="utf-8")
    candidate_path.write_text(json.dumps(report(passed_gate=False, case_passed=False, failures=["boom"])), encoding="utf-8")

    text = format_eval_diff(diff_eval_reports(base_path, candidate_path))

    assert "Agentic Eval Diff" in text
    assert "Gate: PASS -> FAIL" in text
    assert "Regression: YES" in text
    assert "case_1: pass_to_fail REGRESSION" in text


def test_eval_diff_cli_can_fail_on_regression(tmp_path, capsys) -> None:
    base_path = tmp_path / "base.json"
    candidate_path = tmp_path / "candidate.json"
    base_path.write_text(json.dumps(report(passed_gate=True, case_passed=True)), encoding="utf-8")
    candidate_path.write_text(json.dumps(report(passed_gate=False, case_passed=False, failures=["boom"])), encoding="utf-8")

    exit_code = main(
        [
            "--base",
            str(base_path),
            "--candidate",
            str(candidate_path),
            "--fail-on-regression",
        ]
    )

    assert exit_code == 1
    assert "Regression: YES" in capsys.readouterr().out


def report(
    passed_gate: bool,
    case_passed: bool,
    failures: list[str] | None = None,
    tool_success_rate: float = 1.0,
    tool_failures: int = 0,
) -> dict:
    failures = failures or []
    return {
        "total": 1,
        "passed": 1 if case_passed else 0,
        "failed": 0 if case_passed else 1,
        "passedGate": passed_gate,
        "metrics": {
            "case_pass_rate": 1.0 if case_passed else 0.0,
            "tool_calls": 2,
            "tool_failures": tool_failures,
            "tool_success_rate": tool_success_rate,
            "planner_fallbacks": 0,
            "run_failed": 0,
        },
        "eventCounts": {
            "run_started": 1,
            "tool_observation": 2,
        },
        "cases": [
            {
                "name": "case_1",
                "passed": case_passed,
                "failures": failures,
                "status": "completed",
                "tool_names": ["calculator"],
                "response_tiers": ["tool_result_summary"],
                "memory_texts": [],
                "event_counts": {"run_started": 1},
                "metrics": {},
            }
        ],
    }
