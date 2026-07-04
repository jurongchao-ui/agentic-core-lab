"""eval_judge — eval 的回答质量裁判层。

功能:
  - EvalJudgeInput 把一次 case 的期望和实际结果整理成 judge 可读的稳定输入。
  - RuleBasedEvalJudge 提供完全离线、确定性的本地裁判,适合 CI 和学习阶段。
  - LlmEvalJudge 预留 LLM-as-judge 接口,通过 Ollama 输出结构化 JSON,失败时可回退规则裁判。

调用关系图:
  eval_harness.run_eval_case
      └─▶ EvalJudge.judge(EvalJudgeInput)
            ├─▶ RuleBasedEvalJudge(本地确定性)
            └─▶ LlmEvalJudge ─▶ OllamaClient.chat(format_json=True)
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol, runtime_checkable

from agentic_core.runtime.contracts import LlmClient
from agentic_core.llm.json_utils import extract_json_object
from agentic_core.llm.ollama_client import OllamaClient


DEFAULT_JUDGE_RUBRIC_NAME = "agentic_core_default"
DEFAULT_JUDGE_RUBRIC_VERSION = "v1"


@dataclass
class JudgeRubric:
    """judge 评分规则的版本化描述。

    生产里 judge 不是“随便问模型一句”。它必须知道自己按哪一版规则打分,
    否则跨版本趋势无法比较,人工 label 也无法复盘。
    """

    name: str = DEFAULT_JUDGE_RUBRIC_NAME
    version: str = DEFAULT_JUDGE_RUBRIC_VERSION
    min_score: int = 70
    description: str = "status/tools/answer/tier consistency"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "minScore": self.min_score,
            "description": self.description,
        }


@dataclass
class EvalJudgeInput:
    """judge 只看这一份输入,不直接依赖 EvalCase / EvalCaseResult。

    这样可以避免 eval_judge.py 和 eval_harness.py 相互 import。后续如果
    dataset、Web UI、CI 都要调用 judge,也只需要组装这一份输入。
    """

    case_name: str
    goal: str
    expected_status: str
    expected_tools: list[str]
    expected_answer_contains: list[str]
    expected_response_tiers: list[str]
    answer: str
    status: str
    tool_names: list[str]
    response_tiers: list[str]
    deterministic_failures: list[str] = field(default_factory=list)
    judge_rubric: str = DEFAULT_JUDGE_RUBRIC_NAME
    judge_rubric_version: str = DEFAULT_JUDGE_RUBRIC_VERSION
    expected_judge_score: int | None = None
    expected_judge_passed: bool | None = None
    judge_notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "caseName": self.case_name,
            "goal": self.goal,
            "expectedStatus": self.expected_status,
            "expectedTools": list(self.expected_tools),
            "expectedAnswerContains": list(self.expected_answer_contains),
            "expectedResponseTiers": list(self.expected_response_tiers),
            "answer": self.answer,
            "status": self.status,
            "toolNames": list(self.tool_names),
            "responseTiers": list(self.response_tiers),
            "deterministicFailures": list(self.deterministic_failures),
            "judgeRubric": self.judge_rubric,
            "judgeRubricVersion": self.judge_rubric_version,
            "expectedJudgeScore": self.expected_judge_score,
            "expectedJudgePassed": self.expected_judge_passed,
            "judgeNotes": self.judge_notes,
        }


@dataclass
class JudgeDecision:
    """judge 对一条 eval case 的裁判结果。"""

    passed: bool
    score: int
    reason: str
    rubric: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "score": self.score,
            "reason": self.reason,
            "rubric": self.rubric,
            "metadata": dict(self.metadata),
        }


@runtime_checkable
class EvalJudge(Protocol):
    """eval 回答质量裁判协议。"""

    def judge(self, judge_input: EvalJudgeInput) -> JudgeDecision: ...


class RuleBasedEvalJudge:
    """离线确定性 judge。

    它不是“聪明模型”,只按稳定事实打分:
      - 已经有确定性失败时直接失败。
      - 状态、工具、回答片段、response tier 缺失会扣分。

    这个版本的价值是给生产化链路留出 judge 插槽,并且保证 CI 不依赖网络/模型。
    """

    def __init__(self, min_score: int = 70, rubric: JudgeRubric | None = None) -> None:
        self.rubric = rubric or JudgeRubric(min_score=min_score)
        self.min_score = self.rubric.min_score

    def judge(self, judge_input: EvalJudgeInput) -> JudgeDecision:
        score = 100
        reasons: list[str] = []

        if judge_input.deterministic_failures:
            reasons.extend(judge_input.deterministic_failures)
            score = min(score, 40)

        if judge_input.status != judge_input.expected_status:
            score -= 30
            reasons.append(
                f"status expected {judge_input.expected_status}, got {judge_input.status}"
            )

        for tool_name in judge_input.expected_tools:
            if tool_name not in judge_input.tool_names:
                score -= 20
                reasons.append(f"missing expected tool: {tool_name}")

        for text in judge_input.expected_answer_contains:
            if text not in judge_input.answer:
                score -= 25
                reasons.append(f"answer missing expected text: {text}")

        for tier in judge_input.expected_response_tiers:
            if tier not in judge_input.response_tiers:
                score -= 15
                reasons.append(f"missing response tier: {tier}")

        score = max(0, min(100, score))
        passed = score >= self.min_score and not reasons
        return JudgeDecision(
            passed=passed,
            score=score,
            reason="; ".join(reasons) if reasons else "rule judge passed",
            rubric=f"{self.rubric.name}:{self.rubric.version}",
            metadata={
                "source": "rule",
                "minScore": self.min_score,
                "rubric": self.rubric.to_dict(),
            },
        )


class LlmEvalJudge:
    """可选的 LLM-as-judge。

    生产里 LLM judge 需要校准、抽样复核和版本管理。本项目先实现接口骨架:
    要求模型只返回 JSON,并对输出做程序校验; 模型不可用或输出非法时回退规则 judge。
    """

    def __init__(
        self,
        client: LlmClient,
        fallback: EvalJudge | None = None,
        min_score: int = 70,
        rubric: JudgeRubric | None = None,
    ) -> None:
        self.rubric = rubric or JudgeRubric(min_score=min_score)
        self.client = client
        self.fallback = fallback or RuleBasedEvalJudge(rubric=self.rubric)
        self.min_score = self.rubric.min_score

    def judge(self, judge_input: EvalJudgeInput) -> JudgeDecision:
        try:
            raw = self.client.chat(self._messages(judge_input), format_json=True)
            content = str(raw.get("message", {}).get("content", ""))
            decision = self._parse_decision(content)
            decision.metadata.update({"source": "llm", "rawModelOutput": content})
            return decision
        except Exception as error:
            fallback_decision = self.fallback.judge(judge_input)
            fallback_decision.metadata.update(
                {
                    "source": "llm_fallback",
                    "error": str(error),
                }
            )
            return fallback_decision

    def _messages(self, judge_input: EvalJudgeInput) -> list[dict[str, str]]:
        return [
            {
                "role": "system",
                "content": (
                    "You are an eval judge for an agentic runtime. "
                    "Return JSON only with this schema: "
                    "{\"passed\": true|false, \"score\": 0-100, "
                    "\"reason\": \"short reason\", \"rubric\": \"rubric name\"}. "
                    "Judge whether the final answer satisfies the expected behavior. "
                    f"Rubric: {json.dumps(self.rubric.to_dict(), ensure_ascii=False)}"
                ),
            },
            {
                "role": "user",
                "content": json.dumps(judge_input.to_dict(), ensure_ascii=False),
            },
        ]

    def _parse_decision(self, content: str) -> JudgeDecision:
        data = json.loads(extract_json_object(content))
        score = _clamp_int(data.get("score"), default=0)
        passed = bool(data.get("passed")) and score >= self.min_score
        return JudgeDecision(
            passed=passed,
            score=score,
            reason=str(data.get("reason", "")),
            rubric=str(data.get("rubric", f"{self.rubric.name}:{self.rubric.version}")),
            metadata={"minScore": self.min_score, "rubric": self.rubric.to_dict()},
        )


def build_eval_judge(
    mode: str,
    model: str = "openhermes:latest",
    rubric: JudgeRubric | None = None,
) -> EvalJudge | None:
    """按 CLI 参数创建 judge。

    off  = 不启用 judge,保持旧行为。
    rule = 本地确定性 judge。
    llm  = Ollama LLM judge,失败时自动回退 rule judge。
    """

    if mode == "off":
        return None
    if mode == "rule":
        return RuleBasedEvalJudge(rubric=rubric)
    if mode == "llm":
        return LlmEvalJudge(
            OllamaClient(model=model),
            fallback=RuleBasedEvalJudge(rubric=rubric),
            rubric=rubric,
        )
    raise ValueError(f"unknown judge mode: {mode}")


def _clamp_int(value: Any, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(0, min(100, number))
