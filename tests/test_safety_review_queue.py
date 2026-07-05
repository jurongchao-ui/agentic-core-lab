from __future__ import annotations

import json
import re

from agentic_core.runtime.agent import Agent
from agentic_core.memory.store import MemoryStore
from agentic_core.policies.memory import RuleBasedMemoryPolicy
from agentic_core.policies.planner import RuleBasedPlanner
from agentic_core.policies.safety import RuleBasedSafetyPolicy, SafetyRule
from agentic_core.policies.safety_review import (
    InMemorySafetyReviewQueue,
    JsonlSafetyReviewQueue,
    SafetyReviewItem,
    build_safety_review_queue_from_env,
    make_safety_review_item,
)
from agentic_core.tools.registry import ToolRegistry


class BrokenSafetyReviewQueue:
    def enqueue(self, item: SafetyReviewItem) -> SafetyReviewItem:
        raise OSError("review queue unavailable")

    def list_pending(self) -> list[SafetyReviewItem]:
        return []


def test_in_memory_safety_review_queue_enqueues_pending_items() -> None:
    queue = InMemorySafetyReviewQueue()
    item = make_safety_review_item(
        item_id="review_1",
        run_id="run_1",
        goal="需要人工审核",
        safety_decision={"action": "review"},
        identity={"userId": "u1"},
    )

    queued = queue.enqueue(item)

    assert queued == item
    assert queue.list_pending() == [item]


def test_jsonl_safety_review_queue_persists_pending_items(tmp_path) -> None:
    path = tmp_path / "safety-review.jsonl"
    queue = JsonlSafetyReviewQueue(path)
    item = make_safety_review_item(
        item_id="review_1",
        run_id="run_1",
        goal="需要人工审核",
        safety_decision={"action": "review"},
        identity={"userId": "u1"},
    )

    queue.enqueue(item)
    loaded = JsonlSafetyReviewQueue(path).list_pending()

    assert loaded[0].id == "review_1"
    assert loaded[0].goal == "需要人工审核"
    assert json.loads(path.read_text(encoding="utf-8").splitlines()[0])["id"] == "review_1"


def test_build_safety_review_queue_from_env_can_create_jsonl_queue(tmp_path, monkeypatch) -> None:
    path = tmp_path / "queue.jsonl"
    monkeypatch.setenv("AGENTIC_SAFETY_REVIEW_QUEUE", "jsonl")
    monkeypatch.setenv("AGENTIC_SAFETY_REVIEW_QUEUE_PATH", str(path))

    queue = build_safety_review_queue_from_env()

    assert isinstance(queue, JsonlSafetyReviewQueue)
    queue.enqueue(
        make_safety_review_item(
            item_id="review_1",
            run_id="run_1",
            goal="test",
            safety_decision={"action": "review"},
            identity={},
        )
    )
    assert path.exists()


def test_agent_queues_safety_review_decision_and_records_event() -> None:
    memory = MemoryStore()
    memory_policy = RuleBasedMemoryPolicy()
    queue = InMemorySafetyReviewQueue()
    agent = Agent(
        planner=RuleBasedPlanner(),
        tools=ToolRegistry(memory, memory_policy),
        memory=memory,
        memory_policy=memory_policy,
        safety_policy=review_policy(),
        safety_review_queue=queue,
    )

    result = agent.run_typed("这个请求需要人工审核")

    assert result.status == "refused"
    assert result.safety_decision.action == "review"
    assert result.safety_decision.metadata["reviewQueue"]["queued"] is True
    assert len(queue.items) == 1
    assert queue.items[0].goal == "这个请求需要人工审核"
    review_events = [event for event in result.events if event.event_type == "safety_review_queued"]
    assert len(review_events) == 1
    assert review_events[0].payload_schema_valid is True
    assert review_events[0].payload["reviewItem"]["id"] == queue.items[0].id


def test_agent_records_warning_when_safety_review_queue_fails() -> None:
    memory = MemoryStore()
    memory_policy = RuleBasedMemoryPolicy()
    agent = Agent(
        planner=RuleBasedPlanner(),
        tools=ToolRegistry(memory, memory_policy),
        memory=memory,
        memory_policy=memory_policy,
        safety_policy=review_policy(),
        safety_review_queue=BrokenSafetyReviewQueue(),
    )

    result = agent.run_typed("这个请求需要人工审核")

    assert result.status == "refused"
    assert result.safety_decision.metadata["reviewQueue"]["queued"] is False
    review_events = [event for event in result.events if event.event_type == "safety_review_queued"]
    assert review_events[0].level == "warn"
    assert review_events[0].payload_schema_valid is True
    assert review_events[0].payload["reviewItem"]["status"] == "failed"


def review_policy() -> RuleBasedSafetyPolicy:
    return RuleBasedSafetyPolicy(
        rules=[
            SafetyRule(
                rule_id="custom.review",
                category="custom",
                pattern=re.compile(r"人工审核"),
                risk_level="medium",
                confidence=75,
                description="需要人工审核。",
                action="review",
            )
        ]
    )
