from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal


# Literal["tool", "final"] 表示 Action.type 只能是这两个字符串之一。
# 这能帮助初学者明确: planner 的输出不是随便写的文本,而是固定结构。
ActionType = Literal["tool", "final"]


@dataclass
class Action:
    """Planner 输出的标准动作。

    Agent 每一步只接受两类动作:
        1. tool: 调用某个工具
        2. final: 结束任务并回答用户

    @dataclass 会自动生成 __init__ 等方法,让我们少写样板代码。
    """

    type: ActionType
    reason: str
    tool_name: str | None = None
    input: dict[str, Any] = field(default_factory=dict)
    answer: str | None = None
    source: str = "unknown"

    # metadata 存可观测信息,例如 LLM 原始输出、回退原因。不影响执行逻辑。
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def tool(
        cls,
        tool_name: str,
        input: dict[str, Any] | None = None,
        reason: str = "",
        source: str = "unknown",
    ) -> "Action":
        """创建工具动作的快捷方法。

        @classmethod 表示这个方法属于类本身,可以写 Action.tool(...)。
        """
        return cls(
            type="tool",
            tool_name=tool_name,
            input=input or {},
            reason=reason,
            source=source,
        )

    @classmethod
    def final(cls, answer: str, reason: str = "", source: str = "unknown") -> "Action":
        """创建最终回答动作的快捷方法。"""
        return cls(type="final", answer=answer, reason=reason, source=source)

    def to_dict(self) -> dict[str, Any]:
        """把 dataclass 转成普通 dict,方便打印 JSON 和写入 trace。"""
        data = asdict(self)

        # Python 内部用 tool_name,输出给 trace/prompt 时用 toolName,
        # 这样更接近常见 JSON API 风格。
        data["toolName"] = data.pop("tool_name")
        if self.type == "final":
            # final action 不需要 toolName 和 input。
            data.pop("toolName", None)
            data.pop("input", None)
        return data


@dataclass
class Observation:
    """工具执行后的观察结果。

    ok=True 表示工具成功。
    ok=False 表示工具失败,error 里放错误原因。
    """

    ok: bool
    elapsed_ms: int
    output: Any = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class MemoryDecision:
    """MemoryPolicy 的输出结果。"""

    save: bool
    memory_type: str
    text: str
    reason: str
    scores: dict[str, int]
    needs_clarification: bool = False
    clarification_question: str | None = None

    # metadata 存可观测信息,例如 LLM 原始输出、来源(llm/rule/rule_fallback)、回退原因。
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SafetyDecision:
    """SafetyPolicy 的输出: 是否拒绝整轮请求。

    区别于 MemoryPolicy 的“敏感信息不保存”(local safety),
    这是请求级的全局安全拦截(global safety): 命中即拒绝整轮,不评估记忆、不跑 loop。
    """

    refuse: bool
    category: str  # "none" | "malware" | "weapons" | ...
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
