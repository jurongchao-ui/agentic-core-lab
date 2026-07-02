"""middleware — 工具调用的横切中间件管道。

功能:
  - MiddlewarePipeline 在每次工具执行的前/后按顺序运行一组 ToolMiddleware。
  - before_tool 返回 Observation 即"短路"(不执行工具,直接用该结果);
    after_tool 可查看或改写工具执行结果。
  - 内置 ApprovalMiddleware(需审批工具未批准则拦截)、CostAccountingMiddleware(累计成本,不阻断)。
  - 这是审批 / 成本 / 未来限流、重试等生产级横切逻辑的统一挂载点。

调用关系图:
  Agent(Plan-Act-Observe loop, 工具执行处)
      ├─▶ MiddlewarePipeline.before_tool(ctx) ─▶ [Approval → Cost → ...] ─▶ 放行 or 短路 Observation
      ├─▶ ToolRegistry.execute(...)                (仅在放行时执行)
      └─▶ MiddlewarePipeline.after_tool(ctx, obs) ─▶ 逆序过一遍 ─▶ 最终 Observation
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from .schemas import Action, Observation
from .tools import ToolSpec


@dataclass
class ToolCallContext:
    """一次工具调用的中间件上下文。

    Agent 负责构造它;middleware 只读这些信息并决定是否放行、记录成本、
    或在未来触发 human-in-the-loop。
    """

    run_id: str
    step: int
    action: Action
    tool: ToolSpec
    metadata: dict[str, object] = field(default_factory=dict)


class ToolMiddleware(Protocol):
    """工具调用中间件协议。

    before_tool 返回 Observation 时表示短路工具执行。
    after_tool 可以查看或改写工具执行结果。
    """

    def before_tool(self, context: ToolCallContext) -> Observation | None:
        ...

    def after_tool(self, context: ToolCallContext, observation: Observation) -> Observation:
        ...


class MiddlewarePipeline:
    """按顺序执行一组工具中间件。"""

    def __init__(self, middlewares: list[ToolMiddleware] | None = None) -> None:
        self.middlewares = middlewares or []

    @classmethod
    def default(cls) -> "MiddlewarePipeline":
        """默认管道。

        ApprovalMiddleware 是生产级必备边界,但默认工具都不需要审批,
        所以不会改变现有用户行为。
        CostAccountingMiddleware 只记录成本,不阻断。
        """

        return cls([ApprovalMiddleware(), CostAccountingMiddleware()])

    def before_tool(self, context: ToolCallContext) -> Observation | None:
        for middleware in self.middlewares:
            observation = middleware.before_tool(context)
            if observation is not None:
                return observation
        return None

    def after_tool(self, context: ToolCallContext, observation: Observation) -> Observation:
        for middleware in reversed(self.middlewares):
            observation = middleware.after_tool(context, observation)
        return observation


class ApprovalMiddleware:
    """拦截需要人工审批但当前没有审批通过的工具。"""

    def before_tool(self, context: ToolCallContext) -> Observation | None:
        if not context.tool.requires_approval:
            return None
        if context.metadata.get("approved") is True:
            return None
        return Observation(
            ok=False,
            elapsed_ms=0,
            error=f"tool {context.tool.name} requires approval before execution",
        )

    def after_tool(self, context: ToolCallContext, observation: Observation) -> Observation:
        return observation


class CostAccountingMiddleware:
    """记录本轮工具调用成本。

    当前阶段只把 costUnits 写进 context.metadata,后续可以汇总到 AgentRunState、
    budget policy 或 event log。
    """

    def before_tool(self, context: ToolCallContext) -> Observation | None:
        current_value = context.metadata.get("costUnits", 0)
        current_cost = current_value if isinstance(current_value, int) else 0
        context.metadata["costUnits"] = current_cost + context.tool.cost_units
        return None

    def after_tool(self, context: ToolCallContext, observation: Observation) -> Observation:
        return observation
