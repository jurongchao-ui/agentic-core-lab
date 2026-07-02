"""safety_policy — 请求级全局安全拦截(规则版脚手架)。

功能:
  - RuleBasedSafetyPolicy 用一组可注入的 SafetyRule(正则)判断整轮请求是否有害。
  - 命中即返回 refuse=True 的 SafetyDecision,带 category / risk_level / confidence /
    matched_rule / metadata(可解释、可审计)。
  - 结构化满足 contracts.SafetyPolicy; 规则法只是脚手架,日后可经同一协议 drop-in
    LLM/moderation 版(仿 LlmMemoryPolicy 的 LLM+规则兜底)。
  - 区别于 MemoryPolicy 的 local safety(敏感 PII 不保存): 这里是请求级,命中即拒整轮。

调用关系图:
  Agent.run(goal)
      └─▶ SafetyPolicy.check(goal) ─▶ SafetyDecision
            refuse=True: Agent 跳过记忆评估与整个 Plan-Act-Observe loop,
                         ResponsePolicy 用 global_safety 顶档生成拒绝回复。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .schemas import SafetyDecision


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
                refuse=rule.action == "refuse",
                category=rule.category,
                reason=f"命中安全规则 {rule.rule_id}: {rule.description}",
                risk_level=rule.risk_level,
                confidence=rule.confidence,
                matched_rule=rule.rule_id,
                action=rule.action,
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
