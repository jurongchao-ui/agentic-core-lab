from __future__ import annotations

from typing import Any

from agentic_core.runtime.schemas import Action, Observation, TraceStep
from agentic_core.tools.summary import summarize_tool_trace


def test_summarizes_calculator_and_note_success() -> None:
    summary = summarize_tool_trace(
        "帮我计算 128 * 7, 然后记录成学习笔记",
        [
            trace_step(1, "calculator", {"expression": "128 * 7", "result": 896}),
            trace_step(2, "note.add", {"text": "计算 128 * 7 = 896"}),
        ],
    )

    assert summary.success_text == "计算结果是 896。已记录学习笔记：计算 128 * 7 = 896。"
    assert summary.failure_text == ""
    assert "- 计算完成: 128 * 7 = 896" in summary.lines


def test_failed_calculation_suppresses_dependent_note_success() -> None:
    summary = summarize_tool_trace(
        "帮我算 128 / 0，然后记成笔记",
        [
            trace_step(1, "calculator", ok=False, error="division by zero"),
            trace_step(2, "note.add", {"text": "学习笔记: 帮我算 128 / 0"}),
        ],
    )

    assert summary.success_text == ""
    assert "计算失败" in summary.failure_text
    assert "没有记录学习笔记" in summary.failure_text
    assert all("笔记已保存" not in line for line in summary.lines)


def test_summarizes_study_plan() -> None:
    summary = summarize_tool_trace(
        "帮我安排 agentic memory 的学习计划",
        [
            trace_step(
                1,
                "study.plan",
                {
                    "topic": "agentic memory",
                    "maxMinutes": 30,
                    "steps": ["10 分钟: A", "20 分钟: B"],
                },
            )
        ],
    )

    assert "学习计划：agentic memory，总时长不超过 30 分钟" in summary.success_text
    assert "10 分钟: A；20 分钟: B" in summary.success_text
    assert "学习计划已生成" in summary.lines[0]


def trace_step(
    step: int,
    tool_name: str,
    output: Any = None,
    ok: bool = True,
    error: str | None = None,
) -> TraceStep:
    return TraceStep(
        step=step,
        action=Action.tool(tool_name, {}, source="test"),
        observation=Observation(ok=ok, output=output, error=error, elapsed_ms=1),
        created_at="2026-07-02T00:00:00+00:00",
    )
