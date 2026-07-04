"""safety_policy — 请求级全局安全拦截。

功能:
  - RuleBasedSafetyPolicy 用一组可注入的 SafetyRule(正则)判断整轮请求是否有害。
  - 命中即返回 refuse=True 的 SafetyDecision,带 category / risk_level / confidence /
    matched_rule / metadata(可解释、可审计)。
  - LlmSafetyPolicy 可接本地模型或审核服务,输出同一个 SafetyDecision。
  - CompositeSafetyPolicy 可组合多个 checker,选择最高风险结果。
  - 区别于 MemoryPolicy 的 local safety(敏感 PII 不保存): 这里是请求级,命中即拒整轮。

调用关系图:
  Agent.run(goal)
      └─▶ SafetyPolicy.check(goal) ─▶ SafetyDecision
            refuse=True: Agent 跳过记忆评估与整个 Plan-Act-Observe loop,
                         ResponsePolicy 用 global_safety 顶档生成拒绝回复。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import replace
from dataclasses import dataclass
from typing import Any

from agentic_core.runtime.contracts import LlmClient, SafetyPolicy
from agentic_core.llm.json_utils import extract_json_object
from agentic_core.runtime.schemas import SafetyDecision


@dataclass(frozen=True)
class SafetyRule:
    """一条请求级安全规则。

    规则版不是完备安全系统,但要做到结构清楚、可解释、可替换。
    未来 LLM/moderation 版可以继续返回同一个 SafetyDecision。
    """

    rule_id: str
    category: str
    pattern: re.Pattern[str]
    risk_level: str
    confidence: int
    description: str
    action: str = "refuse"


BLOCKING_ACTIONS = {"refuse", "review"}
ACTION_RANK = {
    "allow": 0,
    "warn": 1,
    "review": 2,
    "refuse": 3,
}
RISK_RANK = {
    "none": 0,
    "low": 1,
    "medium": 2,
    "high": 3,
}


SAFETY_RULES = [
    SafetyRule(
        rule_id="malware.ransomware",
        category="malware",
        pattern=re.compile(r"勒索软件|ransomware|木马程序|病毒代码", re.I),
        risk_level="high",
        confidence=95,
        description="请求生成或协助恶意软件。",
    ),
    SafetyRule(
        rule_id="cyber.attack",
        category="cyber_abuse",
        pattern=re.compile(r"(ddos|sql\s*注入).*攻击|攻击.*(网站|服务器)|绕过.*登录|盗取.*账号", re.I),
        risk_level="high",
        confidence=90,
        description="请求实施网络攻击、绕过认证或盗取账号。",
    ),
    SafetyRule(
        rule_id="weapons.explosive",
        category="weapons",
        pattern=re.compile(r"制造.*(炸弹|枪支|爆炸物)|make.*(bomb|explosive)", re.I),
        risk_level="high",
        confidence=95,
        description="请求制造武器或爆炸物。",
    ),
    SafetyRule(
        rule_id="self_harm.method",
        category="self_harm",
        pattern=re.compile(r"自杀方法|怎么自残|如何自杀", re.I),
        risk_level="high",
        confidence=90,
        description="请求自残或自杀方法。",
    ),
]


class RuleBasedSafetyPolicy:
    """请求级全局安全拦截(结构化满足 contracts.SafetyPolicy)。

    区别于 MemoryPolicy 的 local safety(敏感 PII 不保存):
    这里命中即拒绝整轮请求, Agent 会跳过记忆评估和整个 loop。
    """

    def __init__(self, rules: list[SafetyRule] | None = None) -> None:
        self.rules = rules or SAFETY_RULES

    def check(self, text: str) -> SafetyDecision:
        normalized = text.strip()
        for rule in self.rules:
            match = rule.pattern.search(normalized)
            if not match:
                continue
            return SafetyDecision(
                refuse=_is_blocking_action(rule.action),
                category=rule.category,
                reason=f"命中安全规则 {rule.rule_id}: {rule.description}",
                risk_level=rule.risk_level,
                confidence=rule.confidence,
                matched_rule=rule.rule_id,
                action=_normalize_action(rule.action),
                metadata={
                    "source": "rule",
                    "matchedText": match.group(0),
                    "description": rule.description,
                },
            )
        return SafetyDecision(
            refuse=False,
            category="none",
            reason="未命中请求级安全拦截规则。",
            risk_level="none",
            confidence=100,
            action="allow",
            metadata={"source": "rule"},
        )


class LlmSafetyPolicy:
    """LLM/审核服务版 SafetyPolicy。

    生产里常见做法是多层防护:
        1. 规则层快速拦截明确高危请求。
        2. LLM/moderation 层判断更隐晦的意图。
        3. 程序侧统一校验 action/risk/confidence,不直接相信模型输出。

    这个类只负责把模型 JSON 解析成 SafetyDecision。Ollama 不可用或输出非法时,
    回退到 rule_fallback,保证本地学习流程稳定。
    """

    def __init__(
        self,
        client: LlmClient,
        fallback: SafetyPolicy | None = None,
        confidence_threshold: int = 70,
    ) -> None:
        self.client = client
        self.fallback = fallback or RuleBasedSafetyPolicy()
        self.confidence_threshold = confidence_threshold

    def check(self, text: str) -> SafetyDecision:
        content = ""
        try:
            response = self.client.chat(self._messages(text), format_json=True)
            content = str(response.get("message", {}).get("content", ""))
            decision = self._parse_decision(content)
            decision.metadata = {"source": "llm", "rawModelOutput": content}
            return decision
        except Exception as error:
            decision = self.fallback.check(text)
            decision.reason = f"LLM safety policy fallback: {error}. {decision.reason}"
            decision.metadata = {
                "source": "rule_fallback",
                "rawModelOutput": content,
                "error": str(error),
                "fallbackDecision": decision.to_dict(),
            }
            return decision

    def _messages(self, text: str) -> list[dict[str, str]]:
        return [
            {
                "role": "system",
                "content": (
                    "You are a safety classifier for an agentic runtime. "
                    "Return only valid JSON, no markdown. "
                    "Classify whether the user request should be allowed, warned, "
                    "sent to human review, or refused before memory/planner/tools run. "
                    "Schema: {\"action\":\"allow|warn|review|refuse\","
                    "\"category\":\"none|malware|cyber_abuse|weapons|self_harm|privacy|other\","
                    "\"risk_level\":\"none|low|medium|high\","
                    "\"confidence\":0-100,\"matched_rule\":\"optional short id\","
                    "\"reason\":\"short explanation\"}."
                ),
            },
            {"role": "user", "content": text},
        ]

    def _parse_decision(self, content: str) -> SafetyDecision:
        data = json.loads(extract_json_object(content))
        action = _normalize_action(data.get("action"))
        risk_level = _normalize_risk_level(data.get("risk_level") or data.get("riskLevel"))
        confidence = _coerce_confidence(data.get("confidence"))
        category = str(data.get("category") or "none")
        matched_rule = data.get("matched_rule") or data.get("matchedRule")

        # 程序侧兜底: 模型说高风险但 action=allow 时,至少进入 review,不能直接放行。
        if risk_level == "high" and action == "allow":
            action = "review"

        # 低置信度的非高危判断不直接拒绝,交给规则兜底更稳定。
        if confidence < self.confidence_threshold and action in BLOCKING_ACTIONS and risk_level != "high":
            raise ValueError(f"low confidence safety decision: {confidence}")

        return SafetyDecision(
            refuse=_is_blocking_action(action),
            category=category if category else "none",
            reason=str(data.get("reason") or "LLM safety decision."),
            risk_level=risk_level,
            confidence=confidence,
            matched_rule=str(matched_rule) if matched_rule else None,
            action=action,
        )


class CompositeSafetyPolicy:
    """组合多个 SafetyPolicy,选择最高风险结果。

    这模拟生产环境常见的 harness:
        rule checker + LLM checker + 外部 moderation checker。

    所有 checker 都运行,最终选择 action/risk/confidence 排名最高的结果。
    checker 异常默认 fail-open 并记录在 metadata 中;可设置 fail_closed=True
    在审核系统不可用时阻断整轮。
    """

    def __init__(self, policies: list[SafetyPolicy], fail_closed: bool = False) -> None:
        self.policies = policies
        self.fail_closed = fail_closed

    def check(self, text: str) -> SafetyDecision:
        decisions: list[SafetyDecision] = []
        for policy in self.policies:
            try:
                decisions.append(policy.check(text))
            except Exception as error:
                decisions.append(self._checker_error_decision(policy, error))

        if not decisions:
            return SafetyDecision(
                refuse=False,
                category="none",
                reason="未配置 safety checker,默认放行。",
                risk_level="none",
                confidence=0,
                action="allow",
                metadata={"source": "composite", "checks": []},
            )

        selected = max(decisions, key=_decision_rank)
        return replace(
            selected,
            metadata={
                **selected.metadata,
                "source": "composite",
                "selectedSource": selected.metadata.get("source", "unknown"),
                "checks": [decision.to_dict() for decision in decisions],
            },
        )

    def _checker_error_decision(self, policy: SafetyPolicy, error: Exception) -> SafetyDecision:
        action = "refuse" if self.fail_closed else "allow"
        return SafetyDecision(
            refuse=_is_blocking_action(action),
            category="safety_checker_error",
            reason=f"{policy.__class__.__name__} failed: {error}",
            risk_level="medium" if self.fail_closed else "none",
            confidence=0,
            matched_rule="safety.checker_error",
            action=action,
            metadata={
                "source": "checker_error",
                "checker": policy.__class__.__name__,
                "error": str(error),
                "failClosed": self.fail_closed,
            },
        )


def _decision_rank(decision: SafetyDecision) -> tuple[int, int, int]:
    return (
        ACTION_RANK.get(_normalize_action(decision.action), 0),
        RISK_RANK.get(_normalize_risk_level(decision.risk_level), 0),
        decision.confidence,
    )


def _normalize_action(value: Any) -> str:
    action = str(value or "allow").strip().lower()
    return action if action in ACTION_RANK else "allow"


def _normalize_risk_level(value: Any) -> str:
    risk_level = str(value or "none").strip().lower()
    return risk_level if risk_level in RISK_RANK else "none"


def _is_blocking_action(action: str) -> bool:
    return _normalize_action(action) in BLOCKING_ACTIONS


def _coerce_confidence(value: Any) -> int:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0
    if 0 <= parsed <= 1:
        parsed *= 100
    return max(0, min(100, int(parsed)))


def build_safety_policy_from_env(
    model: str = "openhermes:latest",
    client: LlmClient | None = None,
) -> SafetyPolicy:
    """根据环境变量创建 SafetyPolicy。

    默认 `rule`: 完全离线、确定性,保持现有行为。

    可选:
        AGENTIC_SAFETY_POLICY=rule       只用规则。
        AGENTIC_SAFETY_POLICY=llm        只用 LLM,失败回退规则。
        AGENTIC_SAFETY_POLICY=composite  规则 + LLM 并联,选择最高风险结果。
        AGENTIC_SAFETY_FAIL_CLOSED=1     composite checker 失败时阻断整轮。
    """

    mode = os.getenv("AGENTIC_SAFETY_POLICY", "rule").strip().lower()
    rule_policy = RuleBasedSafetyPolicy()
    if mode == "rule":
        return rule_policy

    llm_client = client
    if llm_client is None:
        # 延迟导入,避免规则版默认路径也加载 HTTP client。
        from agentic_core.llm.ollama_client import OllamaClient

        llm_client = OllamaClient(model=model)
    llm_policy = LlmSafetyPolicy(llm_client, fallback=rule_policy)

    if mode == "llm":
        return llm_policy
    if mode == "composite":
        return CompositeSafetyPolicy(
            [rule_policy, llm_policy],
            fail_closed=_optional_bool(os.getenv("AGENTIC_SAFETY_FAIL_CLOSED"), default=False),
        )
    return rule_policy


def _optional_bool(value: str | None, default: bool) -> bool:
    if value is None or not value.strip():
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default
