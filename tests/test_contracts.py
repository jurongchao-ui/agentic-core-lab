from __future__ import annotations

from typing import Any

from agentic_core.runtime.contracts import (
    LlmClient,
    MemoryPolicy,
    Planner,
    Responder,
    ResponsePolicy,
    SafetyPolicy,
)
from agentic_core.policies.memory import LlmMemoryPolicy, RuleBasedMemoryPolicy
from agentic_core.llm.ollama_client import OllamaClient
from agentic_core.policies.planner import HermesPlanner, RuleBasedPlanner
from agentic_core.policies.responder import LlmResponder
from agentic_core.policies.response import RuleBasedResponsePolicy
from agentic_core.policies.safety import CompositeSafetyPolicy, LlmSafetyPolicy, RuleBasedSafetyPolicy


class FakeClient:
    def chat(
        self,
        messages: list[dict[str, str]],
        format_json: bool = False,
    ) -> dict[str, Any]:
        return {"message": {"content": "{}"}}


def test_concrete_impls_satisfy_protocols() -> None:
    """runtime_checkable 冒烟: 现有实现结构化满足对应协议(无需继承)。"""
    assert isinstance(RuleBasedPlanner(), Planner)
    assert isinstance(HermesPlanner(FakeClient()), Planner)
    assert isinstance(RuleBasedMemoryPolicy(), MemoryPolicy)
    assert isinstance(LlmMemoryPolicy(FakeClient()), MemoryPolicy)
    assert isinstance(LlmResponder(FakeClient()), Responder)
    assert isinstance(RuleBasedResponsePolicy(), ResponsePolicy)
    assert isinstance(RuleBasedSafetyPolicy(), SafetyPolicy)
    assert isinstance(LlmSafetyPolicy(FakeClient()), SafetyPolicy)
    assert isinstance(CompositeSafetyPolicy([RuleBasedSafetyPolicy()]), SafetyPolicy)
    assert isinstance(OllamaClient(), LlmClient)
    assert isinstance(FakeClient(), LlmClient)
