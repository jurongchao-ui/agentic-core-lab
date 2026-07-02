from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from .response_policy import ResponseContext, ResponseDecision
from .schemas import Action, MemoryDecision, MemorySnapshot, SafetyDecision, TraceStep


@dataclass
class PlannerContext:
    """Planner 每一步能看到的上下文包。

    现在它是 dataclass,不是裸 dict。Planner 只能读取记忆快照,
    不能直接拿到 MemoryStore 去修改记忆。
    """

    run_id: str
    goal: str
    step: int
    trace: list[TraceStep]
    memory_snapshot: MemorySnapshot
    available_tools: list[dict[str, Any]]


@runtime_checkable
class Planner(Protocol):
    """决定下一步 action。实现: RuleBasedPlanner / HermesPlanner。"""

    def next(self, context: PlannerContext) -> Action: ...


@runtime_checkable
class MemoryPolicy(Protocol):
    """判断一句话是否值得进长期记忆。实现: RuleBasedMemoryPolicy / LlmMemoryPolicy。"""

    def evaluate(self, text: str) -> MemoryDecision: ...


@runtime_checkable
class Responder(Protocol):
    """无工具时的自然语言回复。实现: LlmResponder。"""

    def reply(self, goal: str, memory_snapshot: dict[str, Any]) -> str: ...


@runtime_checkable
class ResponsePolicy(Protocol):
    """最终回复仲裁。实现: RuleBasedResponsePolicy。"""

    def decide(self, context: ResponseContext) -> ResponseDecision: ...


@runtime_checkable
class LlmClient(Protocol):
    """LLM 客户端。实现: OllamaClient(测试用 FakeClient)。"""

    def chat(
        self,
        messages: list[dict[str, str]],
        format_json: bool = False,
    ) -> dict[str, Any]: ...


@runtime_checkable
class SafetyPolicy(Protocol):
    """请求级安全拦截。实现: RuleBasedSafetyPolicy。"""

    def check(self, text: str) -> SafetyDecision: ...
