"""trace_view — 把一次 run 的结果渲染成人读的过程(排障 UI 层)。

功能:
  - resolve_trace_mode(default): 读 AGENTIC_TRACE(off/brief/json),非法值回落 default。
  - format_run_brief(result): 分步可读渲染——记忆决策(llm/回退)、每步动作/工具观察、
    ResponseDecision 的 tiers/reason、最终回答; 回退时补"原因 + 模型原始输出(截断)"。
  - format_run_json(result): 完整 JSON(safety/memory/response/trace/memory 全量)。
  - 纯展示,不改状态; 输入是 Agent.run 返回的 dict。

调用关系图:
  cli / chat ─▶ resolve_trace_mode() 选模式
             ─▶ format_run_brief(result) / format_run_json(result) ─▶ 打印
  数据来源: Agent.run(goal) 的返回 dict(memoryDecision/trace/responseDecision/...)
"""

from __future__ import annotations

import json
import os
from typing import Any


TRACE_MODES = {"off", "brief", "json"}


def resolve_trace_mode(default: str) -> str:
    """读取 AGENTIC_TRACE(off/brief/json),非法值回落到 default。"""
    mode = os.getenv("AGENTIC_TRACE", "").strip().lower()
    return mode if mode in TRACE_MODES else default


def _truncate(text: Any, limit: int = 200) -> str:
    """把任意值转成字符串并截断,避免原始输出刷屏。"""
    s = "" if text is None else str(text)
    s = s.replace("\n", " ")
    return s if len(s) <= limit else s[:limit] + "…"


def _format_memory_line(memory_decision: dict[str, Any]) -> list[str]:
    """渲染记忆决策行,回退时补一行原因 + 模型原始输出。"""
    metadata = memory_decision.get("metadata") or {}
    source = metadata.get("source", "rule")
    lines = [
        f"[记忆] {source} → save={memory_decision.get('save')} "
        f"({memory_decision.get('memory_type')}) {memory_decision.get('text', '')}".rstrip()
    ]
    if source == "rule_fallback":
        lines.append(
            f"        ↳ 回退: {metadata.get('error')}"
            f"；模型原始输出: {_truncate(metadata.get('rawModelOutput'))}"
        )
    return lines


def _format_step(item: dict[str, Any]) -> list[str]:
    """渲染一个 trace step,回退时补一行原因 + 模型原始输出。"""
    action = item.get("action", {})
    observation = item.get("observation", {})
    metadata = action.get("metadata") or {}
    source = action.get("source", "rule")
    tool_name = action.get("toolName")
    header = f"Step {item.get('step')}  planner={source}"
    if tool_name:
        header += f"  {tool_name}  input={action.get('input', {})}"
    else:
        header += "  final"
    lines = [header]
    if metadata.get("error"):
        lines.append(
            f"        ↳ 回退: {metadata.get('error')}"
            f"；模型原始输出: {_truncate(metadata.get('rawModelOutput'))}"
        )
    if observation:
        if observation.get("ok"):
            detail = _truncate(observation.get("output"))
            lines.append(f"        观察: ok  {detail}  ({observation.get('elapsed_ms')}ms)")
        else:
            lines.append(f"        观察: 失败  {observation.get('error')}")
    return lines


def format_run_brief(result: dict[str, Any]) -> str:
    """把 agent.run 的返回渲染成人读的分步过程。"""
    lines: list[str] = []
    if result.get("memoryDecision"):
        lines.extend(_format_memory_line(result["memoryDecision"]))
    for item in result.get("trace", []):
        lines.extend(_format_step(item))
    if result.get("responseDecision"):
        decision = result["responseDecision"]
        lines.append(
            f"[回复] tiers={decision.get('tiers', [])} reason={decision.get('reason', '')}"
        )
    answer = (result.get("answer") or "").splitlines()
    lines.append(f"[回答] {answer[0] if answer else ''}")
    return "\n".join(lines)


def format_run_json(result: dict[str, Any]) -> str:
    """完整 JSON,包含 memoryDecision / trace / snapshot 里的全部细节。"""
    return json.dumps(
        {
            "safetyDecision": result.get("safetyDecision"),
            "memoryDecision": result.get("memoryDecision"),
            "responseDecision": result.get("responseDecision"),
            "trace": result.get("trace"),
            "memory": result.get("memory"),
        },
        ensure_ascii=False,
        indent=2,
    )
