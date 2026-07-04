from __future__ import annotations

import json

from agentic_core.eval_judge_registry import (
    format_judge_rubrics,
    format_rubric_validation,
    get_judge_rubric,
    list_judge_rubrics,
    main,
    validate_dataset_rubrics,
)


def test_get_judge_rubric_returns_registered_version() -> None:
    rubric = get_judge_rubric("strict_answer_quality", "v1")

    assert rubric.name == "strict_answer_quality"
    assert rubric.version == "v1"
    assert rubric.min_score == 90


def test_validate_dataset_rubrics_reports_unknown_rubric() -> None:
    report = validate_dataset_rubrics(
        {
            "cases": [
                {"name": "known", "judgeRubric": "agentic_core_default", "judgeRubricVersion": "v1"},
                {"name": "unknown", "judgeRubric": "new_rubric", "judgeRubricVersion": "v9"},
            ]
        }
    )

    assert report["valid"] is False
    assert report["validCount"] == 1
    assert report["invalid"] == [
        {"caseName": "unknown", "judgeRubric": "new_rubric", "judgeRubricVersion": "v9"}
    ]


def test_format_helpers_include_rubric_names() -> None:
    assert "strict_answer_quality:v1" in format_judge_rubrics(list_judge_rubrics())
    assert "Invalid cases: 1" in format_rubric_validation(
        validate_dataset_rubrics({"cases": [{"name": "bad", "judgeRubric": "bad"}]})
    )


def test_eval_judge_registry_cli_list_and_validate(tmp_path, capsys) -> None:
    dataset_path = tmp_path / "dataset.json"
    dataset_path.write_text(
        json.dumps({"cases": [{"name": "case", "judgeRubric": "strict_answer_quality"}]}),
        encoding="utf-8",
    )

    list_code = main(["list", "--json"])
    validate_code = main(["validate", "--input", str(dataset_path), "--json"])

    output = capsys.readouterr().out
    assert list_code == 0
    assert validate_code == 0
    assert "strict_answer_quality" in output
    assert "agentic_eval_judge_rubric_validation" in output


def test_eval_judge_registry_cli_returns_nonzero_for_unknown_rubric(tmp_path) -> None:
    dataset_path = tmp_path / "dataset.json"
    dataset_path.write_text(
        json.dumps({"cases": [{"name": "case", "judgeRubric": "unknown"}]}),
        encoding="utf-8",
    )

    assert main(["validate", "--input", str(dataset_path), "--json"]) == 1
