from __future__ import annotations

from typing import Any

from agentic_core.agent import Agent
from agentic_core.memory import MemoryStore
from agentic_core.memory_policy import RuleBasedMemoryPolicy
from agentic_core.responder import FALLBACK_REPLY, LlmResponder
from agentic_core.schemas import Action
from agentic_core.tools import ToolRegistry


class FakeClient:
    def __init__(self, content: str | None = None, error: Exception | None = None) -> None:
        self._content = content
        self._error = error

    def chat(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        if self._error is not None:
            raise self._error
        return {"message": {"content": self._content}}


def test_reply_returns_model_text() -> None:
    responder = LlmResponder(FakeClient(content="  你好呀，学习 agentic 不难，慢慢来。 "))
    assert responder.reply("你好", {"longTermMemories": []}) == "你好呀，学习 agentic 不难，慢慢来。"


def test_reply_falls_back_on_error() -> None:
    responder = LlmResponder(FakeClient(error=RuntimeError("Ollama is unavailable")))
    assert responder.reply("你好", {"longTermMemories": []}) == FALLBACK_REPLY


def test_reply_falls_back_on_empty() -> None:
    responder = LlmResponder(FakeClient(content="   "))
    assert responder.reply("你好", {"longTermMemories": []}) == FALLBACK_REPLY


class StubPlanner:
    """总是立刻 final,不调用任何工具(模拟闲聊输入)。"""

    def next(self, context: dict[str, Any]) -> Action:
        return Action.final("模板报告(不该被用户看到)", "no tool needed")


class StubResponder:
    def reply(self, goal: str, memory_snapshot: dict[str, Any]) -> str:
        return f"自然回复: {goal}"


def build_agent(planner: Any, responder: Any) -> Agent:
    memory = MemoryStore()
    policy = RuleBasedMemoryPolicy()
    return Agent(
        planner=planner,
        tools=ToolRegistry(memory, policy),
        memory=memory,
        memory_policy=policy,
        responder=responder,
    )


def test_agent_uses_responder_for_conversational_turn() -> None:
    """本轮没调用工具 => 用 responder 的自然回复,而不是 planner 的任务报告。"""
    agent = build_agent(StubPlanner(), StubResponder())
    result = agent.run("你好，你觉得学习 agentic 难吗")
    assert result["answer"] == "自然回复: 你好，你觉得学习 agentic 难吗"


def test_agent_without_responder_keeps_planner_answer() -> None:
    """没有 responder 时(如离线 demo)保持原有行为。"""
    agent = build_agent(StubPlanner(), None)
    result = agent.run("你好")
    assert result["answer"] == "模板报告(不该被用户看到)"
