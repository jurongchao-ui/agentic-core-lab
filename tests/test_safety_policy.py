from __future__ import annotations

import re

from agentic_core.safety_policy import RuleBasedSafetyPolicy, SafetyRule


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
