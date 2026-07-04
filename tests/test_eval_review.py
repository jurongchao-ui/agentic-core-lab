from __future__ import annotations

import json

import pytest

from agentic_core.eval_harness import load_eval_cases, main as eval_main, run_eval
from agentic_core.eval_review import (
    format_review_agreement,
    format_review_state,
    format_review_status,
    list_review_status,
    main,
    require_reviewed_dataset,
    review_agreement,
    review_dataset,
    review_state,
)


def test_review_dataset_approves_and_rejects_cases() -> None:
    dataset = sample_dataset()

    reviewed = review_dataset(
        dataset,
        approve=["case_ok"],
        reject=["case_bad"],
        reviewer="jr",
        notes="looks stable",
    )

    assert [case["name"] for case in reviewed["cases"]] == ["case_ok"]
    assert reviewed["cases"][0]["reviewRequired"] is False
    assert reviewed["cases"][0]["reviewStatus"] == "approved"
    assert reviewed["cases"][0]["reviewer"] == "jr"
    assert reviewed["reviewSummary"] == {
        "totalCases": 1,
        "approved": 1,
        "pending": 0,
        "rejected": 1,
    }
    assert reviewed["reviewDecisions"][0]["status"] == "approved"
    assert reviewed["reviewDecisions"][1]["status"] == "rejected"


def test_review_dataset_can_apply_judge_labels() -> None:
    reviewed = review_dataset(
        sample_dataset(single_case=True),
        approve_all=True,
        reviewer="jr",
        review_session_id="session_1",
        judge_rubric="strict_answer_quality",
        judge_rubric_version="v1",
        expected_judge_score=95,
        expected_judge_passed=True,
        judge_score_tolerance=4,
        judge_notes="人工标注: 回答完整且清晰",
    )

    case = reviewed["cases"][0]
    assert case["judgeRubric"] == "strict_answer_quality"
    assert case["judgeRubricVersion"] == "v1"
    assert case["expectedJudgeScore"] == 95
    assert case["expectedJudgePassed"] is True
    assert case["judgeScoreTolerance"] == 4
    assert case["judgeNotes"] == "人工标注: 回答完整且清晰"
    assert case["reviewSessionId"] == "session_1"
    assert reviewed["reviewDecisions"][0]["reviewSessionId"] == "session_1"
    assert reviewed["reviewDecisions"][0]["judgeLabels"]["expectedJudgeScore"] == 95


def test_list_review_status_formats_pending_and_approved() -> None:
    reviewed = review_dataset(sample_dataset(), approve=["case_ok"], reviewer="jr")

    statuses = list_review_status(reviewed)
    text = format_review_status(statuses)

    assert statuses[0]["reviewStatus"] == "approved"
    assert statuses[0]["reviewRequired"] is False
    assert statuses[1]["reviewStatus"] == "pending"
    assert "case_ok: approved (ready)" in text
    assert "case_bad: pending (required)" in text


def test_review_agreement_detects_status_and_judge_conflicts() -> None:
    agreement = review_agreement(sample_multi_review_dataset(), score_tolerance=5)

    assert agreement["summary"] == {
        "casesWithReviews": 3,
        "totalReviews": 6,
        "averageReviewers": 2.0,
        "casesWithConflicts": 2,
        "conflictRate": 2 / 3,
    }
    conflict_by_case = {item["caseName"]: item["conflicts"] for item in agreement["cases"]}
    assert conflict_by_case["case_status_conflict"] == ["status_conflict"]
    assert conflict_by_case["case_judge_drift"] == [
        "judge_passed_conflict",
        "judge_score_drift",
    ]
    assert conflict_by_case["case_ok"] == []


def test_format_review_agreement_contains_conflict_summary() -> None:
    text = format_review_agreement(review_agreement(sample_multi_review_dataset(), score_tolerance=5))

    assert "Eval Review Agreement" in text
    assert "Cases with conflicts: 2" in text
    assert "case_judge_drift" in text


def test_review_state_summarizes_multi_user_status() -> None:
    state = review_state(sample_multi_review_dataset(), score_tolerance=5)
    text = format_review_state(state)

    assert state["type"] == "agentic_eval_review_state"
    assert state["summary"]["totalCases"] == 3
    assert state["summary"]["readyCases"] == 1
    assert state["summary"]["conflictCases"] == 2
    assert state["summary"]["uniqueReviewers"] == ["a", "b"]
    assert "session_a" in state["summary"]["reviewSessions"]
    by_case = {item["caseName"]: item for item in state["cases"]}
    assert by_case["case_ok"]["currentStatus"] == "approved"
    assert by_case["case_status_conflict"]["currentStatus"] == "conflict"
    assert by_case["case_judge_drift"]["currentStatus"] == "conflict"
    assert "Eval Review State" in text
    assert "case_status_conflict" in text


def test_require_reviewed_dataset_rejects_pending_cases(tmp_path) -> None:
    path = tmp_path / "dataset.json"
    path.write_text(json.dumps(sample_dataset(), ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ValueError, match="dataset contains unreviewed cases"):
        require_reviewed_dataset(path)


def test_reviewed_dataset_can_feed_eval_with_require_reviewed(tmp_path) -> None:
    dataset = sample_dataset(single_case=True)
    reviewed = review_dataset(dataset, approve_all=True, reviewer="jr")
    path = tmp_path / "golden.json"
    path.write_text(json.dumps(reviewed, ensure_ascii=False), encoding="utf-8")

    require_reviewed_dataset(path)
    report = run_eval(load_eval_cases(path))

    assert report.passed_gate is True
    assert report.total == 1


def test_eval_review_cli_apply_and_list(tmp_path, capsys) -> None:
    input_path = tmp_path / "draft.json"
    output_path = tmp_path / "golden.json"
    input_path.write_text(json.dumps(sample_dataset(single_case=True), ensure_ascii=False), encoding="utf-8")

    apply_code = main(
        [
            "apply",
            "--input",
            str(input_path),
            "--output",
            str(output_path),
            "--approve-all",
            "--reviewer",
            "jr",
            "--judge-rubric",
            "strict_answer_quality",
            "--judge-rubric-version",
            "v1",
            "--expected-judge-score",
            "100",
            "--expected-judge-passed",
            "true",
            "--judge-score-tolerance",
            "2",
            "--judge-notes",
            "cli label",
        ]
    )
    list_code = main(["list", "--input", str(output_path)])

    output = capsys.readouterr().out
    assert apply_code == 0
    assert list_code == 0
    assert "Eval Dataset Review" in output
    assert "approved" in output
    saved = json.loads(output_path.read_text(encoding="utf-8"))
    assert saved["cases"][0]["judgeRubric"] == "strict_answer_quality"
    assert saved["cases"][0]["judgeRubricVersion"] == "v1"
    assert saved["cases"][0]["expectedJudgeScore"] == 100
    assert saved["cases"][0]["expectedJudgePassed"] is True
    assert saved["cases"][0]["judgeScoreTolerance"] == 2
    assert saved["cases"][0]["judgeNotes"] == "cli label"


def test_eval_review_cli_agreement_outputs_json(tmp_path, capsys) -> None:
    path = tmp_path / "multi-review.json"
    path.write_text(json.dumps(sample_multi_review_dataset(), ensure_ascii=False), encoding="utf-8")

    code = main(["agreement", "--input", str(path), "--score-tolerance", "5", "--json"])

    data = json.loads(capsys.readouterr().out)
    assert code == 0
    assert data["type"] == "agentic_eval_review_agreement"
    assert data["summary"]["casesWithConflicts"] == 2


def test_eval_review_cli_state_outputs_json(tmp_path, capsys) -> None:
    path = tmp_path / "multi-review.json"
    path.write_text(json.dumps(sample_multi_review_dataset(), ensure_ascii=False), encoding="utf-8")

    code = main(["state", "--input", str(path), "--score-tolerance", "5", "--json"])

    data = json.loads(capsys.readouterr().out)
    assert code == 0
    assert data["type"] == "agentic_eval_review_state"
    assert data["summary"]["conflictCases"] == 2


def test_eval_harness_require_reviewed_cli_blocks_draft(tmp_path) -> None:
    path = tmp_path / "draft.json"
    path.write_text(json.dumps(sample_dataset(single_case=True), ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ValueError, match="dataset contains unreviewed cases"):
        eval_main(["--cases", str(path), "--require-reviewed"])


def sample_multi_review_dataset() -> dict:
    return {
        "schemaVersion": 1,
        "type": "agentic_eval_dataset",
        "generatedAt": "2026-07-03T00:00:00+00:00",
        "cases": [],
        "reviewDecisions": [
            {
                "caseName": "case_ok",
                "status": "approved",
                "reviewer": "a",
                "reviewSessionId": "session_a",
                "judgeLabels": {"expectedJudgeScore": 95, "expectedJudgePassed": True},
            },
            {
                "caseName": "case_ok",
                "status": "approved",
                "reviewer": "b",
                "reviewSessionId": "session_b",
                "judgeLabels": {"expectedJudgeScore": 98, "expectedJudgePassed": True},
            },
            {
                "caseName": "case_status_conflict",
                "status": "approved",
                "reviewer": "a",
                "reviewSessionId": "session_a",
            },
            {
                "caseName": "case_status_conflict",
                "status": "rejected",
                "reviewer": "b",
                "reviewSessionId": "session_b",
            },
            {
                "caseName": "case_judge_drift",
                "status": "approved",
                "reviewer": "a",
                "reviewSessionId": "session_a",
                "judgeLabels": {"expectedJudgeScore": 70, "expectedJudgePassed": False},
            },
            {
                "caseName": "case_judge_drift",
                "status": "approved",
                "reviewer": "b",
                "reviewSessionId": "session_b",
                "judgeLabels": {"expectedJudgeScore": 95, "expectedJudgePassed": True},
            },
        ],
    }


def sample_dataset(single_case: bool = False) -> dict:
    cases = [
        {
            "name": "case_ok",
            "goal": "帮我计算 128 * 7, 然后记录成学习笔记",
            "reviewRequired": True,
            "expectedStatus": "completed",
            "expectedTools": ["calculator", "note.add"],
            "expectedAnswerContains": ["896", "学习笔记"],
            "expectedMemorySaves": 0,
            "expectedToolFailures": 0,
            "expectedSafetyRefusal": False,
            "expectedResponseTiers": ["tool_result_summary"],
        }
    ]
    if not single_case:
        cases.append(
            {
                "name": "case_bad",
                "goal": "临时观察",
                "reviewRequired": True,
                "expectedStatus": "completed",
            }
        )
    return {
        "schemaVersion": 1,
        "type": "agentic_eval_dataset",
        "generatedAt": "2026-07-03T00:00:00+00:00",
        "source": {"kind": "test"},
        "cases": cases,
    }
