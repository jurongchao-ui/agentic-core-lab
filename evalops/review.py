"""eval_review — eval dataset 的本地审核/标注工具。

event-log-to-eval 生成的是草稿,默认 `reviewRequired=true`。
生产流程里不能把草稿直接当 golden dataset,需要人工确认:

  - 哪些 case 可以进入 golden dataset。
  - 哪些 case 应该丢弃。
  - 谁审核、什么时候审核、审核备注是什么。

本模块提供标准库学习版:
  - list: 查看 dataset 里的 case 审核状态。
  - apply: approve/reject case,写出新的 dataset。
  - approve 后的 case 会设置 reviewRequired=false,reviewStatus=approved。
  - reject 的 case 默认不进入输出 cases,但会写入 reviewDecisions 审计记录。

调用关系图:
  CLI: python -m agentic_core.eval_review (list | apply)
    └─▶ load_dataset ─▶ review_dataset(approve/reject) ─▶ 输出 golden dataset(reviewRequired=false)
    └─▶ list_review_status / format_review_status(查看审核状态)
  require_reviewed_dataset ◀── eval_harness --require-reviewed 调用(拒绝未审核 dataset)
  上游: eval_dataset 生成草稿; 下游: eval_harness 跑已审核 golden dataset。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from agentic_core.memory.store import now_iso


def load_dataset(path: str | Path) -> dict[str, Any]:
    """读取 dataset JSON。"""

    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, list):
        return {
            "schemaVersion": 1,
            "type": "agentic_eval_dataset",
            "cases": data,
            "source": {"kind": "case_list"},
        }
    if not isinstance(data, dict):
        raise ValueError("eval dataset must be a JSON object or case list")
    cases = data.get("cases")
    if not isinstance(cases, list):
        raise ValueError("eval dataset must contain a cases list")
    return data


def review_dataset(
    dataset: dict[str, Any],
    approve: list[str] | None = None,
    reject: list[str] | None = None,
    approve_all: bool = False,
    reviewer: str | None = None,
    review_session_id: str | None = None,
    notes: str | None = None,
    judge_rubric: str | None = None,
    judge_rubric_version: str | None = None,
    expected_judge_score: int | None = None,
    expected_judge_passed: bool | None = None,
    judge_score_tolerance: int | None = None,
    judge_notes: str | None = None,
) -> dict[str, Any]:
    """对 dataset 执行 approve/reject,返回新 dataset。

    reject 的 case 不进入输出 cases,但会进入 reviewDecisions。
    未被 approve/reject 的 case 原样保留。
    """

    approve_names = set(approve or [])
    reject_names = set(reject or [])
    if approve_all and reject_names:
        approve_names = set()
    timestamp = now_iso()
    output = {key: value for key, value in dataset.items() if key != "cases"}
    existing_decisions = dataset.get("reviewDecisions")
    decisions = list(existing_decisions) if isinstance(existing_decisions, list) else []
    reviewed_cases: list[dict[str, Any]] = []

    for case in _cases(dataset):
        name = str(case.get("name", ""))
        if name in reject_names:
            decisions.append(
                _decision(
                    case_name=name,
                    status="rejected",
                    reviewer=reviewer,
                    review_session_id=review_session_id,
                    notes=notes,
                    reviewed_at=timestamp,
                )
            )
            continue
        if approve_all or name in approve_names:
            approved_case = dict(case)
            approved_case["reviewRequired"] = False
            approved_case["reviewStatus"] = "approved"
            approved_case["reviewedAt"] = timestamp
            approved_case["reviewer"] = reviewer
            approved_case["reviewSessionId"] = review_session_id
            approved_case["reviewNotes"] = notes
            _apply_judge_labels(
                approved_case,
                judge_rubric=judge_rubric,
                judge_rubric_version=judge_rubric_version,
                expected_judge_score=expected_judge_score,
                expected_judge_passed=expected_judge_passed,
                judge_score_tolerance=judge_score_tolerance,
                judge_notes=judge_notes,
            )
            reviewed_cases.append(approved_case)
            decisions.append(
                _decision(
                    case_name=name,
                    status="approved",
                    reviewer=reviewer,
                    review_session_id=review_session_id,
                    notes=notes,
                    reviewed_at=timestamp,
                    judge_labels=_judge_labels_for_decision(approved_case),
                )
            )
            continue
        reviewed_cases.append(dict(case))

    output["cases"] = reviewed_cases
    output["reviewedAt"] = timestamp
    output["reviewSummary"] = _review_summary(reviewed_cases, decisions)
    output["reviewDecisions"] = decisions
    return output


def list_review_status(dataset: dict[str, Any]) -> list[dict[str, Any]]:
    """列出 case 审核状态。"""

    statuses: list[dict[str, Any]] = []
    for case in _cases(dataset):
        statuses.append(
            {
                "name": case.get("name", ""),
                "reviewRequired": bool(case.get("reviewRequired", False)),
                "reviewStatus": str(case.get("reviewStatus", "pending")),
                "reviewer": case.get("reviewer"),
                "goal": case.get("goal", ""),
            }
        )
    return statuses


def format_review_status(statuses: list[dict[str, Any]]) -> str:
    """格式化审核状态。"""

    if not statuses:
        return "没有 eval cases。"
    lines = ["Eval Dataset Review"]
    for item in statuses:
        required = "required" if item["reviewRequired"] else "ready"
        lines.append(f"- {item['name']}: {item['reviewStatus']} ({required}) {item['goal']}")
    return "\n".join(lines)


def review_state(dataset: dict[str, Any], score_tolerance: int = 10) -> dict[str, Any]:
    """生成多用户审核状态视图。

    `review_agreement()` 关注“是否一致”。`review_state()` 更像治理后台列表:
    每个 case 当前是什么状态、有哪些 reviewer/session、是否需要继续处理。
    """

    cases = {str(case.get("name", "")): case for case in _cases(dataset) if case.get("name")}
    decisions_by_case = _decisions_by_case(dataset)
    ordered_names = list(cases)
    for case_name in sorted(decisions_by_case):
        if case_name not in cases:
            ordered_names.append(case_name)

    case_states: list[dict[str, Any]] = []
    for case_name in ordered_names:
        case = cases.get(case_name, {})
        decisions = decisions_by_case.get(case_name, [])
        statuses = [str(item.get("status", "")) for item in decisions if item.get("status")]
        reviewers = _reviewers(decisions, fallback=case.get("reviewer"))
        sessions = _review_sessions(decisions, fallback=case.get("reviewSessionId"))
        conflicts = _agreement_conflicts(
            statuses=statuses,
            judge_passed=_judge_passed_values(decisions),
            judge_scores=_judge_scores(decisions),
            score_tolerance=score_tolerance,
        )
        latest = decisions[-1] if decisions else None
        current_status = _current_review_status(case, latest, conflicts)
        case_states.append(
            {
                "caseName": case_name,
                "goal": case.get("goal", ""),
                "currentStatus": current_status,
                "reviewRequired": bool(case.get("reviewRequired", False)),
                "needsReview": current_status in {"pending", "conflict"},
                "reviewers": reviewers,
                "reviewSessions": sessions,
                "reviewCount": len(decisions),
                "statuses": statuses,
                "conflicts": conflicts,
                "latestDecision": dict(latest) if isinstance(latest, dict) else None,
            }
        )

    return {
        "schemaVersion": 1,
        "type": "agentic_eval_review_state",
        "generatedAt": now_iso(),
        "scoreTolerance": score_tolerance,
        "summary": _review_state_summary(case_states),
        "cases": case_states,
    }


def format_review_state(state: dict[str, Any]) -> str:
    """格式化多用户审核状态视图。"""

    raw_summary = state.get("summary")
    summary: dict[str, Any] = raw_summary if isinstance(raw_summary, dict) else {}
    lines = [
        "Eval Review State",
        f"Cases: {summary.get('totalCases', 0)}",
        f"Ready: {summary.get('readyCases', 0)}",
        f"Pending: {summary.get('pendingCases', 0)}",
        f"Conflicts: {summary.get('conflictCases', 0)}",
    ]
    cases = state.get("cases")
    if isinstance(cases, list):
        lines.append("Cases:")
        for item in cases:
            if isinstance(item, dict):
                reviewers = ", ".join(item.get("reviewers", [])) if isinstance(item.get("reviewers"), list) else ""
                lines.append(
                    f"- {item.get('caseName', '')}: {item.get('currentStatus', '')} "
                    f"reviews={item.get('reviewCount', 0)} reviewers={reviewers}"
                )
    return "\n".join(lines)


def review_agreement(dataset: dict[str, Any], score_tolerance: int = 10) -> dict[str, Any]:
    """基于 reviewDecisions 统计多人复核一致性。

    这不是完整协作标注平台,但已经具备生产里最关键的本地信号:
    谁给了什么决策、同一个 case 是否冲突、judge label 分数是否漂移过大。
    """

    case_summaries: list[dict[str, Any]] = []
    for case_name, decisions in _decisions_by_case(dataset).items():
        statuses = [str(item.get("status", "")) for item in decisions if item.get("status")]
        reviewers = sorted(
            {
                str(item.get("reviewer"))
                for item in decisions
                if item.get("reviewer") not in (None, "")
            }
        )
        judge_passed = _judge_passed_values(decisions)
        judge_scores = _judge_scores(decisions)
        conflicts = _agreement_conflicts(
            statuses=statuses,
            judge_passed=judge_passed,
            judge_scores=judge_scores,
            score_tolerance=score_tolerance,
        )
        case_summaries.append(
            {
                "caseName": case_name,
                "reviewCount": len(decisions),
                "reviewers": reviewers,
                "statuses": statuses,
                "statusAgreement": len(set(statuses)) <= 1,
                "judgePassedValues": judge_passed,
                "judgePassedAgreement": len(set(judge_passed)) <= 1,
                "judgeScoreRange": _score_range(judge_scores),
                "conflicts": conflicts,
            }
        )

    case_summaries.sort(key=lambda item: (len(item["conflicts"]) == 0, item["caseName"]))
    conflict_cases = [case for case in case_summaries if case["conflicts"]]
    total_reviews = sum(int(case["reviewCount"]) for case in case_summaries)
    return {
        "schemaVersion": 1,
        "type": "agentic_eval_review_agreement",
        "generatedAt": now_iso(),
        "scoreTolerance": score_tolerance,
        "summary": {
            "casesWithReviews": len(case_summaries),
            "totalReviews": total_reviews,
            "averageReviewers": total_reviews / len(case_summaries) if case_summaries else 0.0,
            "casesWithConflicts": len(conflict_cases),
            "conflictRate": len(conflict_cases) / len(case_summaries) if case_summaries else 0.0,
        },
        "cases": case_summaries,
    }


def format_review_agreement(agreement: dict[str, Any]) -> str:
    """格式化多人复核一致性摘要。"""

    raw_summary = agreement.get("summary")
    summary: dict[str, Any] = raw_summary if isinstance(raw_summary, dict) else {}
    lines = [
        "Eval Review Agreement",
        f"Cases with reviews: {summary.get('casesWithReviews', 0)}",
        f"Cases with conflicts: {summary.get('casesWithConflicts', 0)}",
        f"Conflict rate: {summary.get('conflictRate', 0.0)}",
    ]
    cases = agreement.get("cases")
    if isinstance(cases, list) and cases:
        lines.append("Cases:")
        for item in cases:
            if not isinstance(item, dict):
                continue
            conflicts = item.get("conflicts")
            conflict_text = ", ".join(conflicts) if isinstance(conflicts, list) and conflicts else "ok"
            lines.append(
                f"- {item.get('caseName', '')}: reviews={item.get('reviewCount', 0)} "
                f"conflicts={conflict_text}"
            )
    return "\n".join(lines)


def require_reviewed_dataset(path: str | Path) -> None:
    """确保 dataset 里的 case 都已审核。"""

    dataset = load_dataset(path)
    pending = [
        str(case.get("name", ""))
        for case in _cases(dataset)
        if bool(case.get("reviewRequired", False))
    ]
    if pending:
        raise ValueError(f"dataset contains unreviewed cases: {', '.join(pending)}")


def _decisions_by_case(dataset: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    decisions = dataset.get("reviewDecisions")
    if not isinstance(decisions, list):
        return grouped
    for item in decisions:
        if not isinstance(item, dict):
            continue
        case_name = str(item.get("caseName", ""))
        if not case_name:
            continue
        grouped.setdefault(case_name, []).append(item)
    return grouped


def _judge_passed_values(decisions: list[dict[str, Any]]) -> list[bool]:
    values: list[bool] = []
    for item in decisions:
        labels = item.get("judgeLabels")
        if not isinstance(labels, dict):
            continue
        value = labels.get("expectedJudgePassed")
        if isinstance(value, bool):
            values.append(value)
    return values


def _judge_scores(decisions: list[dict[str, Any]]) -> list[int]:
    scores: list[int] = []
    for item in decisions:
        labels = item.get("judgeLabels")
        if not isinstance(labels, dict):
            continue
        value = labels.get("expectedJudgeScore")
        if value is None:
            continue
        try:
            scores.append(int(value))
        except (TypeError, ValueError):
            continue
    return scores


def _reviewers(decisions: list[dict[str, Any]], fallback: Any = None) -> list[str]:
    values = {
        str(item.get("reviewer"))
        for item in decisions
        if item.get("reviewer") not in (None, "")
    }
    if fallback not in (None, ""):
        values.add(str(fallback))
    return sorted(values)


def _review_sessions(decisions: list[dict[str, Any]], fallback: Any = None) -> list[str]:
    values = {
        str(item.get("reviewSessionId"))
        for item in decisions
        if item.get("reviewSessionId") not in (None, "")
    }
    if fallback not in (None, ""):
        values.add(str(fallback))
    return sorted(values)


def _current_review_status(
    case: dict[str, Any],
    latest_decision: dict[str, Any] | None,
    conflicts: list[str],
) -> str:
    if conflicts:
        return "conflict"
    if isinstance(latest_decision, dict) and latest_decision.get("status"):
        return str(latest_decision["status"])
    if case.get("reviewStatus"):
        return str(case["reviewStatus"])
    return "pending" if case.get("reviewRequired") else "ready"


def _review_state_summary(case_states: list[dict[str, Any]]) -> dict[str, Any]:
    ready = [case for case in case_states if case.get("currentStatus") in {"approved", "ready"}]
    pending = [case for case in case_states if case.get("currentStatus") == "pending"]
    conflicts = [case for case in case_states if case.get("currentStatus") == "conflict"]
    reviewers = sorted(
        {
            reviewer
            for case in case_states
            for reviewer in case.get("reviewers", [])
            if isinstance(reviewer, str)
        }
    )
    sessions = sorted(
        {
            session
            for case in case_states
            for session in case.get("reviewSessions", [])
            if isinstance(session, str)
        }
    )
    return {
        "totalCases": len(case_states),
        "readyCases": len(ready),
        "pendingCases": len(pending),
        "conflictCases": len(conflicts),
        "totalReviewDecisions": sum(int(case.get("reviewCount", 0)) for case in case_states),
        "uniqueReviewers": reviewers,
        "reviewSessions": sessions,
    }


def _agreement_conflicts(
    statuses: list[str],
    judge_passed: list[bool],
    judge_scores: list[int],
    score_tolerance: int,
) -> list[str]:
    conflicts: list[str] = []
    if len(set(statuses)) > 1:
        conflicts.append("status_conflict")
    if len(set(judge_passed)) > 1:
        conflicts.append("judge_passed_conflict")
    score_range = _score_range(judge_scores)
    if score_range is not None and score_range > score_tolerance:
        conflicts.append("judge_score_drift")
    return conflicts


def _score_range(scores: list[int]) -> int | None:
    if not scores:
        return None
    return max(scores) - min(scores)


def _cases(dataset: dict[str, Any]) -> list[dict[str, Any]]:
    cases = dataset.get("cases")
    if not isinstance(cases, list):
        return []
    return [case for case in cases if isinstance(case, dict)]


def _decision(
    case_name: str,
    status: str,
    reviewer: str | None,
    review_session_id: str | None,
    notes: str | None,
    reviewed_at: str,
    judge_labels: dict[str, Any] | None = None,
) -> dict[str, Any]:
    decision: dict[str, Any] = {
        "caseName": case_name,
        "status": status,
        "reviewer": reviewer,
        "reviewSessionId": review_session_id,
        "notes": notes,
        "reviewedAt": reviewed_at,
    }
    if judge_labels:
        decision["judgeLabels"] = judge_labels
    return decision


def _apply_judge_labels(
    case: dict[str, Any],
    judge_rubric: str | None,
    judge_rubric_version: str | None,
    expected_judge_score: int | None,
    expected_judge_passed: bool | None,
    judge_score_tolerance: int | None,
    judge_notes: str | None,
) -> None:
    if judge_rubric is not None:
        case["judgeRubric"] = judge_rubric
    if judge_rubric_version is not None:
        case["judgeRubricVersion"] = judge_rubric_version
    if expected_judge_score is not None:
        case["expectedJudgeScore"] = expected_judge_score
    if expected_judge_passed is not None:
        case["expectedJudgePassed"] = expected_judge_passed
    if judge_score_tolerance is not None:
        case["judgeScoreTolerance"] = judge_score_tolerance
    if judge_notes is not None:
        case["judgeNotes"] = judge_notes


def _judge_labels_for_decision(case: dict[str, Any]) -> dict[str, Any]:
    labels: dict[str, Any] = {}
    for key in [
        "judgeRubric",
        "judgeRubricVersion",
        "expectedJudgeScore",
        "expectedJudgePassed",
        "judgeScoreTolerance",
        "judgeNotes",
    ]:
        if key in case:
            labels[key] = case[key]
    return labels


def _review_summary(cases: list[dict[str, Any]], decisions: list[Any]) -> dict[str, int]:
    approved = 0
    pending = 0
    for case in cases:
        if case.get("reviewStatus") == "approved" and case.get("reviewRequired") is False:
            approved += 1
        elif case.get("reviewRequired"):
            pending += 1
    rejected = 0
    for item in decisions:
        if isinstance(item, dict) and item.get("status") == "rejected":
            rejected += 1
    return {
        "totalCases": len(cases),
        "approved": approved,
        "pending": pending,
        "rejected": rejected,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Review Agentic Core eval dataset cases")
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="列出 dataset case 审核状态")
    list_parser.add_argument("--input", required=True, help="dataset JSON")
    list_parser.add_argument("--json", action="store_true", help="输出 JSON")

    agreement_parser = subparsers.add_parser("agreement", help="统计多人复核一致性")
    agreement_parser.add_argument("--input", required=True, help="dataset JSON")
    agreement_parser.add_argument("--score-tolerance", type=int, default=10, help="judge 分数最大允许差")
    agreement_parser.add_argument("--json", action="store_true", help="输出 JSON")

    state_parser = subparsers.add_parser("state", help="输出多用户审核状态")
    state_parser.add_argument("--input", required=True, help="dataset JSON")
    state_parser.add_argument("--score-tolerance", type=int, default=10, help="judge 分数最大允许差")
    state_parser.add_argument("--json", action="store_true", help="输出 JSON")

    apply_parser = subparsers.add_parser("apply", help="应用 approve/reject 决策")
    apply_parser.add_argument("--input", required=True, help="输入 dataset JSON")
    apply_parser.add_argument("--output", required=True, help="输出 dataset JSON")
    apply_parser.add_argument("--approve", action="append", default=[], help="批准一个 case name,可重复")
    apply_parser.add_argument("--reject", action="append", default=[], help="拒绝一个 case name,可重复")
    apply_parser.add_argument("--approve-all", action="store_true", help="批准所有未拒绝 case")
    apply_parser.add_argument("--reviewer", help="审核人")
    apply_parser.add_argument("--review-session-id", help="审核会话 id")
    apply_parser.add_argument("--notes", help="审核备注")
    apply_parser.add_argument("--judge-rubric", help="给 approved case 写入 judge rubric 名称")
    apply_parser.add_argument("--judge-rubric-version", help="给 approved case 写入 judge rubric 版本")
    apply_parser.add_argument("--expected-judge-score", type=int, help="人工期望 judge 分数")
    apply_parser.add_argument(
        "--expected-judge-passed",
        choices=["true", "false"],
        help="人工期望 judge 是否通过",
    )
    apply_parser.add_argument("--judge-score-tolerance", type=int, help="允许的 judge 分数漂移")
    apply_parser.add_argument("--judge-notes", help="judge 人工标注备注")

    args = parser.parse_args(argv)
    dataset = load_dataset(args.input)
    if args.command == "list":
        statuses = list_review_status(dataset)
        if args.json:
            print(json.dumps(statuses, ensure_ascii=False, indent=2))
        else:
            print(format_review_status(statuses))
        return 0

    if args.command == "agreement":
        agreement = review_agreement(dataset, score_tolerance=args.score_tolerance)
        if args.json:
            print(json.dumps(agreement, ensure_ascii=False, indent=2))
        else:
            print(format_review_agreement(agreement))
        return 0

    if args.command == "state":
        state = review_state(dataset, score_tolerance=args.score_tolerance)
        if args.json:
            print(json.dumps(state, ensure_ascii=False, indent=2))
        else:
            print(format_review_state(state))
        return 0

    reviewed = review_dataset(
        dataset,
        approve=args.approve,
        reject=args.reject,
        approve_all=args.approve_all,
        reviewer=args.reviewer,
        review_session_id=args.review_session_id,
        notes=args.notes,
        judge_rubric=args.judge_rubric,
        judge_rubric_version=args.judge_rubric_version,
        expected_judge_score=args.expected_judge_score,
        expected_judge_passed=_parse_bool_arg(args.expected_judge_passed),
        judge_score_tolerance=args.judge_score_tolerance,
        judge_notes=args.judge_notes,
    )
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(reviewed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


def _parse_bool_arg(value: str | None) -> bool | None:
    if value is None:
        return None
    return value == "true"


if __name__ == "__main__":
    raise SystemExit(main())
