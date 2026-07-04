"""eval_judge_registry — judge rubric 的本地版本注册表。

生产里不能只在 case 里写一个自由文本 `judgeRubric`,否则换了评分规则后,
旧 dataset / 新 judge / 历史 report 之间会失去可比性。

本模块提供标准库学习版:
  - 注册本地已知 rubric。
  - 按 name/version 获取 JudgeRubric。
  - 校验 dataset 中引用的 judgeRubric 是否已登记。

调用关系图:
  eval_harness CLI --judge-rubric/--judge-rubric-version
      └─▶ get_judge_rubric ─▶ RuleBasedEvalJudge / LlmEvalJudge
  eval_judge_registry CLI list/validate
      └─▶ load_dataset ─▶ validate_dataset_rubrics
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from .eval_judge import (
    DEFAULT_JUDGE_RUBRIC_NAME,
    DEFAULT_JUDGE_RUBRIC_VERSION,
    JudgeRubric,
)
from .eval_review import load_dataset


JUDGE_RUBRICS = [
    JudgeRubric(
        name=DEFAULT_JUDGE_RUBRIC_NAME,
        version=DEFAULT_JUDGE_RUBRIC_VERSION,
        min_score=70,
        description="Default deterministic rubric for status/tools/answer/tier consistency.",
    ),
    JudgeRubric(
        name="strict_answer_quality",
        version="v1",
        min_score=90,
        description="Stricter rubric for reviewed golden cases that expect near-perfect answers.",
    ),
]


def list_judge_rubrics() -> list[JudgeRubric]:
    """返回当前本地登记的 judge rubrics。"""

    return list(JUDGE_RUBRICS)


def get_judge_rubric(
    name: str = DEFAULT_JUDGE_RUBRIC_NAME,
    version: str = DEFAULT_JUDGE_RUBRIC_VERSION,
) -> JudgeRubric:
    """按 name/version 获取 rubric。未知版本直接报错,避免静默混用。"""

    for rubric in JUDGE_RUBRICS:
        if rubric.name == name and rubric.version == version:
            return rubric
    raise ValueError(f"unknown judge rubric: {name}:{version}")


def validate_dataset_rubrics(
    dataset: dict[str, Any],
    default_version: str = DEFAULT_JUDGE_RUBRIC_VERSION,
) -> dict[str, Any]:
    """校验 dataset case 里引用的 judgeRubric 是否已登记。"""

    known = {(rubric.name, rubric.version) for rubric in JUDGE_RUBRICS}
    invalid: list[dict[str, str]] = []
    valid_count = 0
    for case in _cases(dataset):
        case_name = str(case.get("name", ""))
        rubric_name = str(case.get("judgeRubric") or DEFAULT_JUDGE_RUBRIC_NAME)
        rubric_version = str(case.get("judgeRubricVersion") or default_version)
        if (rubric_name, rubric_version) in known:
            valid_count += 1
            continue
        invalid.append(
            {
                "caseName": case_name,
                "judgeRubric": rubric_name,
                "judgeRubricVersion": rubric_version,
            }
        )
    return {
        "schemaVersion": 1,
        "type": "agentic_eval_judge_rubric_validation",
        "valid": not invalid,
        "validCount": valid_count,
        "invalidCount": len(invalid),
        "invalid": invalid,
        "knownRubrics": [rubric.to_dict() for rubric in JUDGE_RUBRICS],
    }


def format_judge_rubrics(rubrics: list[JudgeRubric]) -> str:
    """格式化 rubric 注册表。"""

    lines = ["Agentic Judge Rubrics"]
    for rubric in rubrics:
        lines.append(f"- {rubric.name}:{rubric.version} min={rubric.min_score} {rubric.description}")
    return "\n".join(lines)


def format_rubric_validation(report: dict[str, Any]) -> str:
    """格式化 dataset rubric 校验结果。"""

    lines = [
        "Agentic Judge Rubric Validation",
        f"Valid: {report.get('valid', False)}",
        f"Valid cases: {report.get('validCount', 0)}",
        f"Invalid cases: {report.get('invalidCount', 0)}",
    ]
    invalid = report.get("invalid")
    if isinstance(invalid, list) and invalid:
        lines.append("Invalid:")
        for item in invalid:
            if isinstance(item, dict):
                lines.append(
                    f"- {item.get('caseName', '')}: "
                    f"{item.get('judgeRubric', '')}:{item.get('judgeRubricVersion', '')}"
                )
    return "\n".join(lines)


def _cases(dataset: dict[str, Any]) -> list[dict[str, Any]]:
    cases = dataset.get("cases")
    if not isinstance(cases, list):
        return []
    return [case for case in cases if isinstance(case, dict)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage Agentic Core judge rubric registry")
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="列出本地 judge rubric 注册表")
    list_parser.add_argument("--json", action="store_true", help="输出 JSON")

    validate_parser = subparsers.add_parser("validate", help="校验 dataset 引用的 judge rubric")
    validate_parser.add_argument("--input", required=True, help="eval dataset JSON")
    validate_parser.add_argument(
        "--default-version",
        default=DEFAULT_JUDGE_RUBRIC_VERSION,
        help="case 未写 judgeRubricVersion 时使用的默认版本",
    )
    validate_parser.add_argument("--json", action="store_true", help="输出 JSON")

    args = parser.parse_args(argv)
    if args.command == "list":
        rubrics = list_judge_rubrics()
        if args.json:
            print(json.dumps([rubric.to_dict() for rubric in rubrics], ensure_ascii=False, indent=2))
        else:
            print(format_judge_rubrics(rubrics))
        return 0

    dataset = load_dataset(args.input)
    report = validate_dataset_rubrics(dataset, default_version=args.default_version)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(format_rubric_validation(report))
    return 0 if report["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
