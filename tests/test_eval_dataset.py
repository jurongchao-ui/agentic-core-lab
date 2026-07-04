from __future__ import annotations

import json

from agentic_core.runtime.agent import Agent
from evalops.dataset import build_eval_dataset_from_events
from evalops.harness import load_eval_cases, run_eval
from agentic_core.memory.store import MemoryStore
from agentic_core.policies.memory import RuleBasedMemoryPolicy
from agentic_core.policies.planner import RuleBasedPlanner
from agentic_core.tools.registry import ToolRegistry


def test_build_eval_dataset_from_agent_events() -> None:
    result = _run_agent("帮我计算 128 * 7, 然后记录成学习笔记")

    dataset = build_eval_dataset_from_events([event.to_dict() for event in result.events])
    data = dataset.to_dict()

    assert data["schemaVersion"] == 1
    assert data["type"] == "agentic_eval_dataset"
    assert len(data["cases"]) == 1
    case = data["cases"][0]
    assert case["goal"] == "帮我计算 128 * 7, 然后记录成学习笔记"
    assert case["sourceRunId"] == result.run_id
    assert case["reviewRequired"] is True
    assert case["expectedStatus"] == "completed"
    assert case["expectedTools"] == ["calculator", "note.add"]
    assert "896" in case["expectedAnswerContains"]
    assert "学习笔记" in case["expectedAnswerContains"]
    assert case["expectedMemorySaves"] == 0
    assert case["expectedToolFailures"] == 0
    assert case["expectedResponseTiers"] == ["tool_result_summary"]
    assert case["expectedEventCounts"]["tool_observation"] == 2
    assert case["judgeRubric"] == "agentic_core_default"
    assert case["judgeRubricVersion"] == "v1"
    assert case["expectedJudgeScore"] is None
    assert case["expectedJudgePassed"] is None
    assert case["judgeScoreTolerance"] == 10


def test_build_eval_dataset_skips_failed_runs_by_default() -> None:
    failed_events = [
        {
            "type": "run_started",
            "runId": "run_1",
            "payload": {"goal": "会失败的任务"},
        },
        {
            "type": "run_failed",
            "runId": "run_1",
            "payload": {"error": "boom", "errorType": "RuntimeError"},
        },
    ]

    default_dataset = build_eval_dataset_from_events(failed_events)
    include_failed_dataset = build_eval_dataset_from_events(failed_events, include_failed=True)

    assert default_dataset.cases == []
    assert len(include_failed_dataset.cases) == 1
    assert include_failed_dataset.cases[0].expected_status == "failed"


def test_load_eval_cases_preserves_zero_and_false_expectations(tmp_path) -> None:
    path = tmp_path / "dataset.json"
    path.write_text(
        json.dumps(
            {
                "cases": [
                    {
                        "name": "zero_case",
                        "goal": "你好",
                        "expectedMemorySaves": 0,
                        "expectedToolFailures": 0,
                        "expectedSafetyRefusal": False,
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    cases = load_eval_cases(path)

    assert cases[0].expected_memory_saves == 0
    assert cases[0].expected_tool_failures == 0
    assert cases[0].expected_safety_refusal is False


def test_generated_dataset_can_feed_eval_harness(tmp_path) -> None:
    result = _run_agent("帮我计算 128 * 7, 然后记录成学习笔记")
    dataset = build_eval_dataset_from_events([event.to_dict() for event in result.events])
    path = tmp_path / "dataset.json"
    path.write_text(json.dumps(dataset.to_dict(), ensure_ascii=False), encoding="utf-8")

    report = run_eval(load_eval_cases(path))

    assert report.passed_gate is True
    assert report.total == 1
    assert report.cases[0].passed is True


def _run_agent(goal: str):
    memory = MemoryStore()
    policy = RuleBasedMemoryPolicy()
    agent = Agent(
        planner=RuleBasedPlanner(),
        tools=ToolRegistry(memory, policy),
        memory=memory,
        memory_policy=policy,
        responder=None,
    )
    return agent.run_typed(goal)
