from __future__ import annotations

from typing import Any

from agentic_core.eval_judge import (
    EvalJudgeInput,
    JudgeDecision,
    JudgeRubric,
    LlmEvalJudge,
    RuleBasedEvalJudge,
    build_eval_judge,
)


def test_rule_judge_passes_when_expected_answer_is_present() -> None:
    judge = RuleBasedEvalJudge()

    decision = judge.judge(
        EvalJudgeInput(
            case_name="calculator",
            goal="帮我计算 128 * 7",
            expected_status="completed",
            expected_tools=["calculator"],
            expected_answer_contains=["896"],
            expected_response_tiers=[],
            answer="计算结果是 896。",
            status="completed",
            tool_names=["calculator"],
            response_tiers=[],
        )
    )

    assert decision.passed is True
    assert decision.score == 100
    assert decision.metadata["source"] == "rule"
    assert decision.metadata["rubric"]["version"] == "v1"


def test_rule_judge_fails_when_expected_answer_is_missing() -> None:
    judge = RuleBasedEvalJudge()

    decision = judge.judge(
        EvalJudgeInput(
            case_name="calculator",
            goal="帮我计算 128 * 7",
            expected_status="completed",
            expected_tools=["calculator"],
            expected_answer_contains=["896"],
            expected_response_tiers=[],
            answer="计算完成。",
            status="completed",
            tool_names=["calculator"],
            response_tiers=[],
        )
    )

    assert decision.passed is False
    assert decision.score == 75
    assert "896" in decision.reason


def test_llm_judge_parses_model_json() -> None:
    judge = LlmEvalJudge(FakeClient('{"passed": true, "score": 91, "reason": "ok", "rubric": "answer quality"}'))

    decision = judge.judge(sample_input())

    assert decision.passed is True
    assert decision.score == 91
    assert decision.rubric == "answer quality"
    assert decision.metadata["source"] == "llm"


def test_llm_judge_falls_back_when_model_output_is_invalid() -> None:
    judge = LlmEvalJudge(FakeClient("not json"), fallback=RuleBasedEvalJudge())

    decision = judge.judge(sample_input())

    assert decision.passed is True
    assert decision.metadata["source"] == "llm_fallback"
    assert "error" in decision.metadata


def test_build_eval_judge_supports_off_and_rule() -> None:
    assert build_eval_judge("off") is None
    assert isinstance(build_eval_judge("rule"), RuleBasedEvalJudge)


def test_rule_judge_uses_versioned_rubric() -> None:
    judge = RuleBasedEvalJudge(rubric=JudgeRubric(name="strict", version="v2", min_score=90))

    decision = judge.judge(sample_input())

    assert decision.rubric == "strict:v2"
    assert decision.metadata["rubric"] == {
        "name": "strict",
        "version": "v2",
        "minScore": 90,
        "description": "status/tools/answer/tier consistency",
    }


def test_judge_input_includes_human_label_fields() -> None:
    judge_input = sample_input()
    judge_input.expected_judge_score = 95
    judge_input.expected_judge_passed = True
    judge_input.judge_notes = "人工确认回答清楚"

    data = judge_input.to_dict()

    assert data["judgeRubric"] == "agentic_core_default"
    assert data["judgeRubricVersion"] == "v1"
    assert data["expectedJudgeScore"] == 95
    assert data["expectedJudgePassed"] is True
    assert data["judgeNotes"] == "人工确认回答清楚"


def sample_input() -> EvalJudgeInput:
    return EvalJudgeInput(
        case_name="memory",
        goal="以后安排学习任务时，每次控制在30分钟以内",
        expected_status="completed",
        expected_tools=[],
        expected_answer_contains=["30分钟"],
        expected_response_tiers=[],
        answer="已记住: 以后学习任务控制在 30分钟以内。",
        status="completed",
        tool_names=[],
        response_tiers=[],
    )


class FakeClient:
    def __init__(self, content: str) -> None:
        self.content = content

    def chat(
        self,
        messages: list[dict[str, str]],
        format_json: bool = False,
    ) -> dict[str, Any]:
        return {"message": {"content": self.content}}


class AlwaysFailJudge:
    def judge(self, judge_input: EvalJudgeInput) -> JudgeDecision:
        return JudgeDecision(
            passed=False,
            score=10,
            reason=f"forced failure for {judge_input.case_name}",
            rubric="test",
        )
