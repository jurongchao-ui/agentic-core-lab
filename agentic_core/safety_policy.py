from __future__ import annotations

import re

from .schemas import SafetyDecision


# 起步规则集。诚实地说这是脚手架: 关键词/正则对有害内容判定粗糙、易绕过。
# v1 交付的是"能拒整轮的全局拦截 + Protocol seam", 而不是完备的有害内容检测。
# 真实安全需 LLM 分类器 / moderation API, 日后经 contracts.SafetyPolicy 协议 drop-in
# (可仿 LlmMemoryPolicy 的 LLM + 规则兜底结构)。
HARMFUL_PATTERNS: dict[str, re.Pattern[str]] = {
    "malware": re.compile(
        r"勒索软件|ransomware|木马程序|病毒代码|(ddos|sql\s*注入).*攻击|攻击.*(网站|服务器)",
        re.I,
    ),
    "weapons": re.compile(r"制造.*(炸弹|枪支|爆炸物)|make.*(bomb|explosive)", re.I),
    "self_harm": re.compile(r"自杀方法|怎么自残|如何自杀", re.I),
}


class RuleBasedSafetyPolicy:
    """请求级全局安全拦截(结构化满足 contracts.SafetyPolicy)。

    区别于 MemoryPolicy 的 local safety(敏感 PII 不保存):
    这里命中即拒绝整轮请求, Agent 会跳过记忆评估和整个 loop。
    """

    def check(self, text: str) -> SafetyDecision:
        for category, pattern in HARMFUL_PATTERNS.items():
            if pattern.search(text):
                return SafetyDecision(
                    refuse=True,
                    category=category,
                    reason=f"命中安全类别: {category}",
                )
        return SafetyDecision(refuse=False, category="none", reason="")
