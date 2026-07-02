from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from .schemas import MemoryDecision, MemoryRecord, MemorySnapshot, SafetyDecision, TraceStep
from .tool_summary import summarize_tool_trace


@dataclass
class ResponseContext:
    """ResponsePolicy 做最终回复判断时需要的上下文。

    把参数收进一个 dataclass,比 decide(goal, trace, memory...) 更适合扩展。
    后面如果要加入 user_profile、intent_split 等字段,只改这里,不用把函数签名越拉越长。
    """

    goal: str
    memory_decision: MemoryDecision
    saved_memories: list[MemoryRecord]
    trace: list[TraceStep]
    planner_answer: str | None
    incomplete_reason: str | None
    memory_snapshot: MemorySnapshot
    responder: Any | None = None
    safety_decision: SafetyDecision | None = None


@dataclass
class ResponseDecision:
    """ResponsePolicy 的可审计输出。"""

    text: str
    tiers: list[str]
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class RuleBasedResponsePolicy:
    """最终回复仲裁层。

    它不负责规划动作,也不自由发挥文采。它只决定:
    - 是否该追问
    - 是否该说明敏感信息未保存
    - 是否该确认长期记忆
    - 是否该总结工具 observation
    - 是否该回落到 planner / responder
    """

    def decide(self, context: ResponseContext) -> ResponseDecision:
        # 0. global safety: 请求级拦截,优先级最高,命中即拒绝整轮。
        if context.safety_decision and context.safety_decision.refuse:
            return ResponseDecision(
                text="抱歉，这个请求我无法帮助。",
                tiers=["global_safety"],
                reason=f"SafetyPolicy 拒绝: {context.safety_decision.category}",
            )

        # 1. clarification 是当前学习版的全局拦截。
        if context.memory_decision.needs_clarification:
            question = (
                context.memory_decision.clarification_question
                or "请告诉我你想保存的具体内容。"
            )
            return ResponseDecision(
                text=question,
                tiers=["clarification"],
                reason="MemoryPolicy 需要用户补充信息,当前阶段采用全局追问。",
            )

        parts: list[str] = []
        tiers: list[str] = []
        reasons: list[str] = []

        # 2. local safety: 敏感记忆只拒绝保存,不等价于整轮危险。
        if self._is_sensitive_memory_rejection(context.memory_decision):
            parts.append("这类信息不适合进入长期记忆，我不会保存。")
            tiers.append("local_safety")
            reasons.append("MemoryPolicy 判断输入包含敏感信息风险。")

        # 3. memory confirmation: 只确认真实写入的长期记忆。
        if context.saved_memories:
            memory_texts = [memory.text for memory in context.saved_memories if memory.text]
            if memory_texts:
                parts.append("已记住：" + "；".join(memory_texts) + "。")
                tiers.append("memory_confirmation")
                reasons.append(f"本轮实际写入 {len(memory_texts)} 条长期记忆。")

        # 4. tool result summary + failure: 只根据 trace/observation 说话。
        tool_summary = summarize_tool_trace(context.goal, context.trace)
        if tool_summary.success_text:
            parts.append(tool_summary.success_text)
            tiers.append("tool_result_summary")
            reasons.append("根据成功的 tool observation 汇总结果。")
        if tool_summary.failure_text:
            parts.append(tool_summary.failure_text)
            tiers.append("failure_incomplete")
            reasons.append("根据失败 observation 或未完成原因据实说明。")

        # 5. max_steps / 未完成也要显式说明。
        if context.incomplete_reason:
            parts.append(context.incomplete_reason)
            if "failure_incomplete" not in tiers:
                tiers.append("failure_incomplete")
            reasons.append("Agent loop 未正常完成。")

        if parts:
            return ResponseDecision(text="".join(parts), tiers=tiers, reason=" ".join(reasons))

        # 6. planner answer: LLM planner 直接 final 时的兜底。
        if context.planner_answer:
            return ResponseDecision(
                text=context.planner_answer,
                tiers=["planner_answer"],
                reason="没有命中内容档,使用 planner 提供的 final.answer。",
            )

        # 7. normal responder: 普通聊天/解释才交给 responder。
        if context.responder is not None:
            return ResponseDecision(
                text=context.responder.reply(context.goal, context.memory_snapshot.to_dict()),
                tiers=["normal_responder"],
                reason="没有工具结果和记忆动作,使用 responder 生成普通回复。",
            )

        return ResponseDecision(
            text="我可以帮你计算、记笔记、管理待办,或记住你的长期偏好。你想做什么?",
            tiers=["normal_responder"],
            reason="没有可用 responder,使用默认能力引导语。",
        )

    def _is_sensitive_memory_rejection(self, decision: MemoryDecision) -> bool:
        """判断本轮是否因为敏感信息而拒绝长期记忆。

        只认结构化信号 sensitivity_risk(规则版和 LLM 版都会写),
        不靠 reason 文案匹配——文案一改就静默失效。
        """
        return not decision.save and decision.scores.get("sensitivity_risk", 0) >= 3
