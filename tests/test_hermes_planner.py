from __future__ import annotations

from typing import Any

from agentic_core.contracts import PlannerContext
from agentic_core.memory import MemoryStore
from agentic_core.planner import HermesPlanner
from agentic_core.schemas import Action
from agentic_core.tools import ToolRegistry
from agentic_core.memory_policy import RuleBasedMemoryPolicy


class FakeClient:
    def __init__(self, content: str) -> None:
        self.content = content
        self.format_json_calls: list[bool] = []

    def chat(
        self,
        messages: list[dict[str, str]],
        format_json: bool = False,
    ) -> dict[str, Any]:
        self.format_json_calls.append(format_json)
        return {"message": {"content": self.content}}


def test_hermes_planner_requests_json_format() -> None:
    client = FakeClient('{"type":"final","answer":"可以。","reason":"done"}')
    memory = MemoryStore()
    policy = RuleBasedMemoryPolicy()
    context = PlannerContext(
        run_id="run_1",
        goal="你好",
        step=1,
        trace=[],
        memory_snapshot=memory.snapshot(),
        available_tools=ToolRegistry(memory, policy).list(),
    )

    action = HermesPlanner(client, fallback=StaticFallback()).next(context)

    assert client.format_json_calls == [True]
    assert action.source == "hermes"


class StaticFallback:
    def next(self, context: PlannerContext) -> Action:
        return Action.final("fallback", reason="fallback", source="fallback")
