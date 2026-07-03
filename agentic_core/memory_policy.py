"""memory_policy — 判断一句话是否值得进长期记忆(结构化满足 contracts.MemoryPolicy)。

功能:
  - 不是用户说的每句话都值得记, 且**敏感信息一票否决**(密码/密钥/证件号, 程序侧正则拦, 不靠模型)。
  - 两个实现共用 evaluate(text) -> MemoryDecision 契约, 装配层用 AGENTIC_MEMORY_POLICY 切换:
      RuleBasedMemoryPolicy: 纯正则按维度打分(future_relevance/stability/user_preference/…),
                             正向分达阈值且不敏感才存; 确定性、离线; 也是 LLM 版的兜底。
      LlmMemoryPolicy:       LLM 语义抽取 + 程序把关(敏感一票否决 + 置信度阈值 + 类型校验),
                             Ollama 不可用/输出非法时回退规则版; 捕获 rawModelOutput 供排障。
  - coerce_confidence: 容忍模型把 confidence 写成 null/非数字/0-1 小数, 永不抛异常。
  - SENSITIVE_PATTERN: 全项目共享的敏感词真相源(tools 守卫、event 脱敏都复用它)。

调用关系图:
  Agent.run ─▶ MemoryPolicy.evaluate(goal) ─▶ MemoryDecision(save/type/text/scores/…)
  LlmMemoryPolicy.evaluate ─▶ LlmClient.chat(format_json) ─▶ extract_json_object ─▶ 程序 gate
                            └─(异常/非法)─▶ RuleBasedMemoryPolicy.evaluate(兜底)
  MemoryDecision 下游: Agent 决定是否 add_long_term_memory; ResponsePolicy 据 sensitivity_risk 判 local_safety。
"""

from __future__ import annotations

import json
import re
from typing import Any

from .contracts import LlmClient
from .json_utils import extract_json_object
from .schemas import MemoryDecision


TECH_STACK_PATTERN = re.compile(
    r"技术栈|stack|常用技术|Node\.js|React|Python|FastAPI|Codex",
    re.I,
)
EXPLICIT_MEMORY_PATTERN = re.compile(
    r"记住|保存|计入长期记忆|加入长期记忆|长期记忆|以后记得",
    re.I,
)
TECH_STACK_VALUE_PATTERN = re.compile(
    r"(?:我的)?(?:技术栈|stack|常用技术)(?:是|包括|有|:|：)\s*(.+)",
    re.I,
)
# 敏感信息真相源: rule 版和 llm 版共用同一个模式,保证“不该长期保存的信息”只有一处定义。
SENSITIVE_PATTERN = re.compile(
    r"密码|密钥|token|银行卡|身份证|账号|验证码|api[_ -]?key|password|secret|private[_ -]?key|access[_ -]?key|cookie|credential",
    re.I,
)


class RuleBasedMemoryPolicy:
    """决定“要不要把这句话写入长期记忆”的规则层。

    一个重要原则:
        不是用户说的每句话都值得保存。

    例子:
        “我今天有点累”        -> 临时状态,通常不保存。
        “以后学习任务控制30分钟” -> 长期偏好,会影响未来决策,应该保存。
        “我的技术栈是 Node.js 和 React” -> 稳定用户资料,应该保存。

    为什么不用模型直接决定?
        因为长期记忆会影响未来行为,必须可控、可解释、可调试。
        模型可以帮助理解语义,但最终保存规则应该由程序掌握。

    注意:
        显式保存意图不是万能保存。
        用户给了具体内容才保存;只说“请记住我的技术栈”但没说技术栈是什么,就追问。
    """

    # 正向分达到 7 才保存。
    # 正向分 = future_relevance + stability + user_preference + task_continuity
    #        + explicit_memory_intent + user_profile
    SAVE_THRESHOLD = 7

    def evaluate(self, text: str) -> MemoryDecision:
        """对一段用户输入做评分,返回 MemoryDecision。

        MemoryDecision 里包括:
            save: 是否保存
            memory_type: 保存类型,例如 preference
            text: 真正保存的文本
            reason: 为什么保存/不保存
            scores: 每个维度的分数
            needs_clarification: 是否需要追问用户补充信息
        """

        # strip() 去掉用户输入前后的空格和换行。
        normalized = text.strip()

        # 每个维度单独打分,这样结果更容易解释。
        scores = {
            "future_relevance": self._future_relevance(normalized),
            "stability": self._stability(normalized),
            "user_preference": self._user_preference(normalized),
            "task_continuity": self._task_continuity(normalized),
            "explicit_memory_intent": self._explicit_memory_intent(normalized),
            "user_profile": self._user_profile(normalized),
            "sensitivity_risk": self._sensitivity_risk(normalized),
        }

        extracted_memory = self._extract_memory_text(normalized)

        # 敏感信息优先级最高。即使用户明确说“请记住”,也不能保存密码/密钥。
        if scores["sensitivity_risk"] >= 3:
            return MemoryDecision(
                save=False,
                memory_type="none",
                text="",
                reason=self._reason(False, 0, scores),
                scores=scores,
            )

        # 用户明确要求保存某类资料,但没有提供具体内容时,不要让模型编造。
        if self._needs_clarification(normalized, extracted_memory):
            return MemoryDecision(
                save=False,
                memory_type="none",
                text="",
                reason="用户明确要求保存长期记忆,但缺少可保存的具体内容。",
                scores=scores,
                needs_clarification=True,
                clarification_question=self._clarification_question(normalized),
            )

        # sensitivity_risk 是风险分,不算进正向分。
        # 例如密码、密钥、身份证这类信息,即使未来有用,也不应该随便长期保存。
        positive_score = (
            scores["future_relevance"]
            + scores["stability"]
            + scores["user_preference"]
            + scores["task_continuity"]
            + scores["explicit_memory_intent"]
            + scores["user_profile"]
        )

        # 保存条件:
        # 1. 长期价值足够高
        # 2. 敏感风险不高
        save = positive_score >= self.SAVE_THRESHOLD

        # 如果不保存,type 就是 none,text 就是空字符串。
        memory_type = self._memory_type(normalized, scores) if save else "none"
        memory_text = (
            self._normalize_memory(extracted_memory or normalized, memory_type)
            if save
            else ""
        )

        return MemoryDecision(
            save=save,
            memory_type=memory_type,
            text=memory_text,
            reason=self._reason(save, positive_score, scores),
            scores=scores,
        )

    def _future_relevance(self, text: str) -> int:
        """未来相关性: 这句话会不会影响以后怎么做?"""
        if re.search(r"以后|下次|未来|每次|始终|默认|记住|以后.*安排|长期记忆", text):
            return 3
        if re.search(r"今天|现在|刚刚|临时|有点累", text):
            return 0
        return 1

    def _stability(self, text: str) -> int:
        """稳定性: 这是长期偏好,还是只针对今天/这次?"""
        if re.search(r"以后|每次|默认|习惯|偏好|控制在|不要|尽量|技术栈|常用技术", text):
            return 2
        if re.search(r"今天|今晚|这次|临时|有点累", text):
            return 0
        return 1

    def _user_preference(self, text: str) -> int:
        """用户偏好: 是否表达了喜欢/不喜欢/默认规则/约束?"""
        if re.search(r"我喜欢|我不喜欢|偏好|习惯|默认|以后|每次|控制在|不要|尽量", text):
            return 3
        return 0

    def _task_continuity(self, text: str) -> int:
        """任务连续性: 是否能帮助未来继续同一类任务?"""
        if re.search(r"学习|任务|安排|计划|提醒|待办|项目|agentic|课程|技术栈|Codex", text, re.I):
            return 2
        return 0

    def _explicit_memory_intent(self, text: str) -> int:
        """显式保存意图: 用户是否明确要求写入记忆。"""
        if EXPLICIT_MEMORY_PATTERN.search(text):
            return 3
        return 0

    def _user_profile(self, text: str) -> int:
        """用户资料: 是否包含稳定的用户背景资料,例如技术栈。"""
        if TECH_STACK_PATTERN.search(text):
            return 3
        if re.search(r"我是|我的职业|我的岗位|我主要用|我常用", text):
            return 2
        return 0

    def _sensitivity_risk(self, text: str) -> int:
        """敏感风险: 是否包含不应该长期保存的信息?"""
        if SENSITIVE_PATTERN.search(text):
            return 5
        return 0

    def _memory_type(self, text: str, scores: dict[str, int]) -> str:
        """给长期记忆分类。分类方便未来检索和使用。"""
        if scores["user_profile"] >= 2:
            return "user_profile"
        if scores["user_preference"] >= 2:
            return "preference"
        if "学习" in text or "任务" in text:
            return "task_context"
        return "note"

    def _normalize_memory(self, text: str, memory_type: str) -> str:
        """把用户原话整理成更适合保存的记忆文本。"""
        if memory_type == "user_profile":
            tech_stack = self._extract_tech_stack(text)
            if tech_stack:
                return f"用户技术栈: {tech_stack}"
            if TECH_STACK_PATTERN.search(text):
                return f"用户技术栈: {text}"
            return f"用户资料: {text}"
        if memory_type == "preference":
            return f"用户偏好: {text}"
        if memory_type == "task_context":
            return f"任务上下文: {text}"
        return text

    def _reason(self, save: bool, positive_score: int, scores: dict[str, int]) -> str:
        """生成给人看的解释,方便调试 MemoryPolicy。"""
        if scores["sensitivity_risk"] >= 3:
            return "包含敏感信息风险,不进入长期记忆。"
        if save:
            return f"长期价值分 {positive_score} 达到阈值,会影响未来决策。"
        return f"长期价值分 {positive_score} 未达到阈值,更像临时状态或一次性信息。"

    def _extract_memory_text(self, text: str) -> str:
        """提取真正适合保存的内容。

        例如:
            请记住我的技术栈是 Python、FastAPI、React
        会提取:
            Python、FastAPI、React
        """
        tech_stack = self._extract_tech_stack(text)
        if tech_stack:
            return tech_stack
        return text

    def _extract_tech_stack(self, text: str) -> str:
        """从一句话里抽取技术栈内容。"""
        match = TECH_STACK_VALUE_PATTERN.search(text)
        if not match:
            return ""
        raw_value = match.group(1).strip()
        raw_value = re.sub(r"[。!！?？].*$", "", raw_value).strip()
        return self._normalize_list_text(raw_value)

    def _normalize_list_text(self, text: str) -> str:
        """把 'Node.js 和 React，Codex' 整理成 'Node.js、React、Codex'。"""
        text = re.sub(r"\s*和\s*", "、", text)
        text = re.sub(r"\s*,\s*", "、", text)
        text = re.sub(r"\s*，\s*", "、", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text.strip("、,， ")

    def _needs_clarification(self, text: str, extracted_memory: str) -> bool:
        """判断是否应该追问。

        用户明确要求保存某类资料,但没有提供具体内容时,追问。
        例如“请把我的技术栈计入长期记忆里”。
        """
        has_explicit_intent = self._explicit_memory_intent(text) > 0
        asks_about_tech_stack = bool(re.search(r"技术栈|stack|常用技术", text, re.I))
        has_tech_stack_value = bool(self._extract_tech_stack(text))
        return has_explicit_intent and asks_about_tech_stack and not has_tech_stack_value

    def _clarification_question(self, text: str) -> str:
        """生成追问问题。"""
        if re.search(r"技术栈|stack|常用技术", text, re.I):
            return "可以，请告诉我你的技术栈具体包括哪些？"
        return "可以，请告诉我你想保存的具体内容是什么？"


def coerce_confidence(value: Any, default: int) -> int:
    """把模型给的 confidence 安全转成 0-100 的 int。

    本地小模型经常把 confidence 写成 null、"high" 或 0-1 小数。
    绝不能让这种格式问题抛异常,进而把整条抽取拖进脆弱的规则兜底。
    解析不了就用 default(阈值),即“不因格式问题误伤一次有效抽取”。
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if 0 < number <= 1:  # 有的模型用 0-1 量纲,归一到 0-100
        number *= 100
    return max(0, min(100, int(number)))


class LlmMemoryPolicy:
    """用 LLM 做语义记忆抽取的策略,程序保留最终 gate。

    结构和 planner.py 的 HermesPlanner 一模一样:
        LLM 提议 -> 程序校验/把关 -> 失败则回退规则版。

    为什么语义判断交给 LLM?
        “这句话算不算长期偏好/用户画像”是语义问题,正则靠关键词命中会误判
        (例如“用 Python 算一下”不该被当成用户技术栈)。

    为什么还要程序 gate?
        因为长期记忆会影响未来行为。最关键的一条——敏感信息拦截——
        必须由程序用正则做,不能寄托于模型自觉。安全边界不交给 LLM。
    """

    # 允许的记忆类型。模型给出别的类型就判为非法,触发 fallback。
    ALLOWED_TYPES = {"preference", "user_profile", "task_context", "note", "none"}

    # 置信度阈值。模型说要保存但置信度不够,就不保存。
    CONFIDENCE_THRESHOLD = 60

    def __init__(
        self, client: LlmClient, fallback: RuleBasedMemoryPolicy | None = None
    ) -> None:
        self.client = client
        self.fallback = fallback or RuleBasedMemoryPolicy()

    def evaluate(self, text: str) -> MemoryDecision:
        """成功路径: text -> prompt -> Ollama -> JSON -> 程序 gate -> MemoryDecision。

        失败路径: 任何异常(Ollama 不可用/非法 JSON/字段不合法) -> 回退规则版。
        """
        # 先把 content 置空,这样即使解析失败,回退时也能把模型原文带进 metadata。
        content: str | None = None
        try:
            raw = self.client.chat(self._messages(text), format_json=True)
            content = raw.get("message", {}).get("content", "")
            decision = self._parse_decision(content, text)
            decision.metadata = {"source": "llm", "rawModelOutput": content}
            return decision
        except Exception as error:
            decision = self.fallback.evaluate(text)
            decision.reason = f"LLM memory policy fallback: {error}. {decision.reason}"
            decision.metadata = {
                "source": "rule_fallback",
                "rawModelOutput": content,
                "error": str(error),
            }
            return decision

    def _messages(self, text: str) -> list[dict[str, str]]:
        return [
            {
                "role": "system",
                "content": (
                    "You extract long-term memories from a single user message. "
                    "Return only valid JSON, no markdown. "
                    "Decide whether the message contains information worth remembering "
                    "across sessions (stable preferences, user profile, ongoing task context). "
                    "Do NOT save one-off or temporary states. "
                    "Do NOT save secrets such as passwords, api keys or ID numbers; "
                    "mark those with sensitive=true. "
                    "Schema: {\"save\":true,\"type\":\"preference|user_profile|task_context|note|none\","
                    "\"text\":\"concise memory to store\",\"sensitive\":false,\"confidence\":0-100,"
                    "\"needs_clarification\":false,\"clarification_question\":\"\",\"reason\":\"...\"}."
                ),
            },
            {"role": "user", "content": text},
        ]

    def _parse_decision(self, content: str, text: str) -> MemoryDecision:
        """把模型输出解析成 MemoryDecision,并做程序把关(顺序即优先级)。"""
        data = json.loads(extract_json_object(content))
        confidence = coerce_confidence(data.get("confidence"), default=self.CONFIDENCE_THRESHOLD)
        scores = {"confidence": confidence}

        # 1. 敏感一票否决: 模型标记 或 程序正则命中,任一即拦。安全不依赖模型。
        # 写入稳定的 sensitivity_risk 信号(和规则版一致),下游 ResponsePolicy 据此判断,
        # 不用靠 reason 文案里恰好有“敏感”两个字。
        if bool(data.get("sensitive")) or SENSITIVE_PATTERN.search(text):
            return MemoryDecision(
                save=False,
                memory_type="none",
                text="",
                reason="包含敏感信息风险,不进入长期记忆。",
                scores={**scores, "sensitivity_risk": 5},
            )

        # 2. 需要追问: 模型判断用户想存但没给具体内容时透传给 Agent。
        memory_text = str(data.get("text", "")).strip()
        if bool(data.get("needs_clarification")) and not memory_text:
            return MemoryDecision(
                save=False,
                memory_type="none",
                text="",
                reason=str(data.get("reason", "缺少可保存的具体内容。")),
                scores=scores,
                needs_clarification=True,
                clarification_question=str(
                    data.get("clarification_question") or "请告诉我你想保存的具体内容。"
                ),
            )

        # 3. 类型校验: 非法类型抛错 -> 触发 fallback。
        memory_type = str(data.get("type", "none"))
        if memory_type not in self.ALLOWED_TYPES:
            raise ValueError(f"unknown memory type: {memory_type}")

        # 保存条件: 模型要求保存 + 有内容 + 类型不是 none + 置信度达标。
        save = (
            bool(data.get("save"))
            and bool(memory_text)
            and memory_type != "none"
            and confidence >= self.CONFIDENCE_THRESHOLD
        )
        return MemoryDecision(
            save=save,
            memory_type=memory_type if save else "none",
            text=memory_text if save else "",
            reason=str(data.get("reason", "")),
            scores=scores,
        )
