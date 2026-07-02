from __future__ import annotations

from typing import Any, Protocol, TypedDict, runtime_checkable

from .memory import MemoryStore
from .response_policy import ResponseContext, ResponseDecision
from .schemas import Action, MemoryDecision, SafetyDecision


class PlannerContext(TypedDict):
    """Planner 每一步能看到的上下文包。

    以前是裸 dict[str, Any],现在用 TypedDict 明确有哪些键,
    这样 planner 内部 context["goal"] 之类访问能被类型检查。
    """

    run_id: str
    goal: str
    step: int
    trace: list[dict[str, Any]]
    memory: MemoryStore
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

    def chat(self, messages: list[dict[str, str]]) -> dict[str, Any]: ...


@runtime_checkable
class SafetyPolicy(Protocol):
    """请求级安全拦截。实现: RuleBasedSafetyPolicy。"""

    def check(self, text: str) -> SafetyDecision: ...
