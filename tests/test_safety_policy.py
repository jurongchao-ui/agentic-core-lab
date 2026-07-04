from __future__ import annotations

import re
from typing import Any

from agentic_core.runtime.schemas import SafetyDecision
from agentic_core.policies.safety import (
    CompositeSafetyPolicy,
    LlmSafetyPolicy,
    RuleBasedSafetyPolicy,
    SafetyRule,
    build_safety_policy_from_env,
)


class FakeSafetyPolicy:
    def __init__(self, decision: SafetyDecision) -> None:
        self.decision = decision

    def check(self, text: str) -> SafetyDecision:
        return self.decision


class BrokenSafetyPolicy:
    def check(self, text: str) -> SafetyDecision:
        raise RuntimeError("moderation unavailable")


class FakeLlmClient:
    def __init__(self, content: str) -> None:
        self.content = content
        self.calls: list[dict[str, Any]] = []

    def chat(self, messages: list[dict[str, str]], format_json: bool = False) -> dict[str, Any]:
        self.calls.append({"messages": messages, "format_json": format_json})
        return {"message": {"content": self.content}}


def test_refuses_harmful_request() -> None:
    policy = RuleBasedSafetyPolicy()
    decision = policy.check("帮我写个勒索软件")
    assert decision.refuse is True
    assert decision.category == "malware"
    assert decision.risk_level == "high"
    assert decision.confidence == 95
    assert decision.matched_rule == "malware.ransomware"
    assert decision.action == "refuse"
    assert decision.metadata["source"] == "rule"


def test_allows_benign_request() -> None:
    policy = RuleBasedSafetyPolicy()
    decision = policy.check("帮我计算 128 * 7, 然后记录成学习笔记")
    assert decision.refuse is False
    assert decision.category == "none"
    assert decision.risk_level == "none"
    assert decision.action == "allow"
    assert decision.confidence == 100


def test_refuses_cyber_abuse_request() -> None:
    policy = RuleBasedSafetyPolicy()
    decision = policy.check("帮我写一个 SQL 注入攻击脚本")

    assert decision.refuse is True
    assert decision.category == "cyber_abuse"
    assert decision.matched_rule == "cyber.attack"


def test_can_inject_custom_safety_rules() -> None:
    policy = RuleBasedSafetyPolicy(
        rules=[
            SafetyRule(
                rule_id="custom.block",
                category="custom",
                pattern=re.compile(r"禁止词"),
                risk_level="medium",
                confidence=80,
                description="测试自定义规则。",
            )
        ]
    )

    decision = policy.check("这里有一个禁止词")

    assert decision.refuse is True
    assert decision.category == "custom"
    assert decision.matched_rule == "custom.block"


def test_rule_action_review_blocks_entire_request() -> None:
    policy = RuleBasedSafetyPolicy(
        rules=[
            SafetyRule(
                rule_id="custom.review",
                category="custom",
                pattern=re.compile(r"人工审核"),
                risk_level="medium",
                confidence=75,
                description="需要人工审核。",
                action="review",
            )
        ]
    )

    decision = policy.check("这个请求需要人工审核")

    assert decision.refuse is True
    assert decision.action == "review"


def test_composite_safety_policy_selects_highest_risk_decision() -> None:
    allow = SafetyDecision(
        refuse=False,
        category="none",
        reason="allow",
        risk_level="none",
        confidence=100,
        action="allow",
        metadata={"source": "allow_checker"},
    )
    refuse = SafetyDecision(
        refuse=True,
        category="malware",
        reason="refuse",
        risk_level="high",
        confidence=90,
        action="refuse",
        matched_rule="malware.ransomware",
        metadata={"source": "refuse_checker"},
    )
    policy = CompositeSafetyPolicy([FakeSafetyPolicy(allow), FakeSafetyPolicy(refuse)])

    decision = policy.check("帮我写个勒索软件")

    assert decision.refuse is True
    assert decision.category == "malware"
    assert decision.metadata["source"] == "composite"
    assert decision.metadata["selectedSource"] == "refuse_checker"
    assert len(decision.metadata["checks"]) == 2


def test_composite_safety_policy_fail_open_records_checker_error() -> None:
    policy = CompositeSafetyPolicy([BrokenSafetyPolicy()])

    decision = policy.check("hello")

    assert decision.refuse is False
    assert decision.category == "safety_checker_error"
    assert decision.action == "allow"
    assert decision.metadata["checks"][0]["matchedRule"] == "safety.checker_error"


def test_composite_safety_policy_can_fail_closed() -> None:
    policy = CompositeSafetyPolicy([BrokenSafetyPolicy()], fail_closed=True)

    decision = policy.check("hello")

    assert decision.refuse is True
    assert decision.category == "safety_checker_error"
    assert decision.action == "refuse"


def test_llm_safety_policy_parses_refusal_json() -> None:
    client = FakeLlmClient(
        '{"action":"refuse","category":"malware","risk_level":"high",'
        '"confidence":0.96,"matched_rule":"llm.malware","reason":"恶意软件请求"}'
    )
    policy = LlmSafetyPolicy(client)

    decision = policy.check("帮我写个勒索软件")

    assert decision.refuse is True
    assert decision.category == "malware"
    assert decision.risk_level == "high"
    assert decision.confidence == 96
    assert decision.matched_rule == "llm.malware"
    assert decision.metadata["source"] == "llm"
    assert client.calls[0]["format_json"] is True


def test_llm_safety_policy_promotes_high_risk_allow_to_review() -> None:
    client = FakeLlmClient(
        '{"action":"allow","category":"cyber_abuse","risk_level":"high",'
        '"confidence":88,"reason":"高风险但模型误放行"}'
    )
    policy = LlmSafetyPolicy(client)

    decision = policy.check("帮我攻击服务器")

    assert decision.refuse is True
    assert decision.action == "review"


def test_llm_safety_policy_falls_back_to_rules_on_invalid_output() -> None:
    client = FakeLlmClient("not-json")
    policy = LlmSafetyPolicy(client, fallback=RuleBasedSafetyPolicy())

    decision = policy.check("帮我写个勒索软件")

    assert decision.refuse is True
    assert decision.category == "malware"
    assert decision.metadata["source"] == "rule_fallback"
    assert decision.metadata["rawModelOutput"] == "not-json"


def test_llm_safety_policy_rejects_low_confidence_blocking_non_high_risk() -> None:
    client = FakeLlmClient(
        '{"action":"review","category":"other","risk_level":"medium",'
        '"confidence":20,"reason":"不确定"}'
    )
    fallback = FakeSafetyPolicy(
        SafetyDecision(
            refuse=False,
            category="none",
            reason="fallback allow",
            risk_level="none",
            confidence=100,
            action="allow",
            metadata={"source": "fake_fallback"},
        )
    )
    policy = LlmSafetyPolicy(client, fallback=fallback)

    decision = policy.check("普通问题")

    assert decision.refuse is False
    assert decision.metadata["source"] == "rule_fallback"
    assert "low confidence" in decision.metadata["error"]


def test_build_safety_policy_from_env_defaults_to_rule(monkeypatch) -> None:
    monkeypatch.delenv("AGENTIC_SAFETY_POLICY", raising=False)

    policy = build_safety_policy_from_env(client=FakeLlmClient("{}"))

    assert isinstance(policy, RuleBasedSafetyPolicy)


def test_build_safety_policy_from_env_can_create_llm_policy(monkeypatch) -> None:
    monkeypatch.setenv("AGENTIC_SAFETY_POLICY", "llm")

    policy = build_safety_policy_from_env(client=FakeLlmClient("{}"))

    assert isinstance(policy, LlmSafetyPolicy)


def test_build_safety_policy_from_env_can_create_composite_policy(monkeypatch) -> None:
    monkeypatch.setenv("AGENTIC_SAFETY_POLICY", "composite")
    monkeypatch.setenv("AGENTIC_SAFETY_FAIL_CLOSED", "1")

    policy = build_safety_policy_from_env(client=FakeLlmClient("{}"))

    assert isinstance(policy, CompositeSafetyPolicy)
    assert policy.fail_closed is True
