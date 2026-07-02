from __future__ import annotations

import re
from dataclasses import dataclass, field

from .schemas import TraceStep


@dataclass
class ToolTraceSummary:
    """工具执行结果的共享摘要。

    ResponsePolicy 用 success_text/failure_text 生成用户可见回复。
    RuleBasedPlanner 的 build_answer 用 lines 生成兜底/debug 风格回复。
    """

    success_text: str = ""
    failure_text: str = ""
    lines: list[str] = field(default_factory=list)


def summarize_tool_trace(goal: str, trace: list[TraceStep]) -> ToolTraceSummary:
    """根据 trace 汇总工具执行结果。

    这是工具 observation -> 文案的单一真相源,避免 ResponsePolicy 和 Planner
    各写一套,以后新增工具时只改这里。
    """

    successes: list[str] = []
    failures: list[str] = []
    lines: list[str] = []

    failed_calculator = _first_failed_tool(trace, "calculator")
    successful_calculator = _first_successful_tool(trace, "calculator")
    goal_needs_note = bool(re.search(r"记录|笔记|note", goal, re.I))
    goal_has_arithmetic = bool(re.search(r"(\d+(?:\s*[+\-*/%]\s*\d+)+)", goal))
    note_depends_on_failed_calculation = bool(
        failed_calculator and goal_has_arithmetic and goal_needs_note and not successful_calculator
    )

    for item in trace:
        action = item.action
        observation = item.observation
        tool_name = action.tool_name
        if not observation.ok:
            if tool_name == "calculator" and note_depends_on_failed_calculation:
                failure = f"计算失败：{observation.error}，因此没有记录学习笔记。"
                line = f"- calculator 失败: {observation.error}; 未记录依赖该计算的学习笔记"
            else:
                failure = f"{tool_name} 执行失败：{observation.error}。"
                line = f"- {tool_name} 失败: {observation.error}"
            failures.append(failure)
            lines.append(line)
            continue

        output = observation.output
        if tool_name == "calculator":
            successes.append(f"计算结果是 {output['result']}。")
            lines.append(f"- 计算完成: {output['expression']} = {output['result']}")
        elif tool_name == "note.add" and not note_depends_on_failed_calculation:
            successes.append(f"已记录学习笔记：{output['text']}。")
            lines.append(f"- 笔记已保存: {output['text']}")
        elif tool_name == "todo.add":
            successes.append(f"已添加待办：{output['text']}。")
            lines.append(f"- 待办已添加: {output['text']}")
        elif tool_name == "todo.list":
            if output:
                todos_cn = "；".join(f"{todo['id']}:{todo['text']}" for todo in output)
                todos_debug = "; ".join(f"{todo['id']}:{todo['text']}" for todo in output)
                successes.append(f"当前待办：{todos_cn}。")
                lines.append(f"- 当前待办: {todos_debug}")
            else:
                successes.append("当前没有待办。")
                lines.append("- 当前待办: 当前没有待办")
        elif tool_name == "study.plan":
            steps = "；".join(output.get("steps", []))
            successes.append(
                f"学习计划：{output['topic']}，总时长不超过 {output['maxMinutes']} 分钟。"
                f"{steps}。"
            )
            lines.append(
                f"- 学习计划已生成: {output['topic']} "
                f"(总时长不超过 {output['maxMinutes']} 分钟): {steps}"
            )
        elif tool_name == "memory.add":
            if output.get("saved"):
                lines.append(f"- 长期记忆已保存: {output['memory']['text']}")
            else:
                successes.append(f"长期记忆未保存：{output.get('reason')}。")
                lines.append(f"- 长期记忆未保存(未通过记忆策略): {output.get('reason')}")

    return ToolTraceSummary(
        success_text="".join(successes),
        failure_text="".join(failures),
        lines=lines,
    )


def _first_successful_tool(trace: list[TraceStep], tool_name: str) -> TraceStep | None:
    for item in trace:
        if item.action.tool_name == tool_name and item.observation.ok:
            return item
    return None


def _first_failed_tool(trace: list[TraceStep], tool_name: str) -> TraceStep | None:
    for item in trace:
        if item.action.tool_name == tool_name and not item.observation.ok:
            return item
    return None
