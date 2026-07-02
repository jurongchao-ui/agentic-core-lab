from __future__ import annotations

from typing import Any

from agentic_core.contracts import (
    LlmClient,
    MemoryPolicy,
    Planner,
    Responder,
    ResponsePolicy,
)
from agentic_core.memory_policy import LlmMemoryPolicy, RuleBasedMemoryPolicy
from agentic_core.ollama_client import OllamaClient
from agentic_core.planner import HermesPlanner, RuleBasedPlanner
from agentic_core.responder import LlmResponder
from agentic_core.response_policy import RuleBasedResponsePolicy


class FakeClient:
    def chat(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        return {"message": {"content": "{}"}}


def test_concrete_impls_satisfy_protocols() -> None:
    """runtime_checkable 冒烟: 现有实现结构化满足对应协议(无需继承)。"""
    assert isinstance(RuleBasedPlanner(), Planner)
    assert isinstance(HermesPlanner(FakeClient()), Planner)
    assert isinstance(RuleBasedMemoryPolicy(), MemoryPolicy)
    assert isinstance(LlmMemoryPolicy(FakeClient()), MemoryPolicy)
    assert isinstance(LlmResponder(FakeClient()), Responder)
    assert isinstance(RuleBasedResponsePolicy(), ResponsePolicy)
    assert isinstance(OllamaClient(), LlmClient)
    assert isinstance(FakeClient(), LlmClient)
