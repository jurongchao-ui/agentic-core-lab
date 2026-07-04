"""middleware — 工具调用的横切中间件管道。

功能:
  - MiddlewarePipeline 在每次工具执行的前/后按顺序运行一组 ToolMiddleware。
  - before_tool 返回 Observation 即"短路"(不执行工具,直接用该结果);
    after_tool 可查看或改写工具执行结果。
  - MiddlewarePipeline.execute_tool 把 before / 真正执行 / timeout / retry / after 收进同一条链。
  - 内置 ApprovalMiddleware(需审批工具未批准则拦截)、CostAccountingMiddleware(累计成本,不阻断)。
  - 这是审批 / 成本 / timeout / retry / tracing / idempotency 等生产级横切逻辑的统一挂载点。

调用关系图:
  Agent(Plan-Act-Observe loop, 工具执行处)
      ├─▶ MiddlewarePipeline.before_tool(ctx) ─▶ [Approval → Cost → ...] ─▶ 放行 or 短路 Observation
      ├─▶ MiddlewarePipeline.execute_tool(...)     (timeout / retry / tracing / idempotency)
      └─▶ MiddlewarePipeline.after_tool(ctx, obs) ─▶ 逆序过一遍 ─▶ 最终 Observation
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from agentic_core.runtime.context import RuntimeIdentity
from agentic_core.runtime.schemas import Action, Observation
from agentic_core.tools.registry import RiskLevel, SideEffect, ToolSpec


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
    identity: RuntimeIdentity = field(default_factory=RuntimeIdentity)
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass
class ToolGovernancePolicy:
    """工具执行治理策略。

    ToolSpec 描述工具自身属性;ToolGovernancePolicy 描述当前运行环境允许什么。
    生产里可以按租户、环境、用户角色注入不同策略。
    """

    allowed_permission_scopes: set[str] | None = None
    denied_permission_scopes: set[str] = field(default_factory=set)
    require_approval_for_risk_levels: set[RiskLevel] = field(default_factory=lambda: {"high"})
    require_approval_for_side_effects: set[SideEffect] = field(default_factory=set)
    max_cost_units_per_run: int | None = None


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

        return cls([ToolGovernanceMiddleware(), CostAccountingMiddleware()])

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

    def execute_tool(self, context: ToolCallContext, execute: Callable[[], Any]) -> Observation:
        """执行一次工具调用,并统一套上生产级横切逻辑。

        学习版做四件事:
            1. before_tool 可短路,例如未审批工具不执行。
            2. 根据 ToolSpec.timeout_ms 做超时控制。
            3. 根据 ToolSpec.retry_count 做失败重试。
            4. 给 Observation.metadata 写入 tracing/idempotency 审计信息。
        """

        started_at = time.time()
        context.metadata.setdefault("toolName", context.tool.name)
        context.metadata.setdefault("timeoutMs", context.tool.timeout_ms)
        context.metadata.setdefault("retryCount", context.tool.retry_count)
        context.metadata.setdefault("idempotencyKey", _idempotency_key(context))
        context.metadata.setdefault("permissionScope", context.tool.permission_scope)
        context.metadata.setdefault("sideEffect", context.tool.side_effect)
        context.metadata.setdefault("riskLevel", context.tool.risk_level)
        context.metadata.setdefault("identity", context.identity.to_dict())

        short_circuit = self.before_tool(context)
        if short_circuit is not None:
            short_circuit.metadata.update(_observation_metadata(context, started_at, attempts=0, short_circuited=True))
            return self.after_tool(context, short_circuit)

        attempts = max(1, context.tool.retry_count + 1)
        observation: Observation | None = None
        for attempt in range(1, attempts + 1):
            context.metadata["attempt"] = attempt
            try:
                output = _execute_with_timeout(execute, context.tool.timeout_ms)
                observation = Observation(
                    ok=True,
                    output=output,
                    elapsed_ms=int((time.time() - started_at) * 1000),
                )
                break
            except TimeoutError as error:
                observation = Observation(
                    ok=False,
                    error=str(error),
                    elapsed_ms=int((time.time() - started_at) * 1000),
                )
            except Exception as error:
                observation = Observation(
                    ok=False,
                    error=str(error),
                    elapsed_ms=int((time.time() - started_at) * 1000),
                )

            if attempt < attempts:
                context.metadata["lastError"] = observation.error or ""
                continue

        if observation is None:  # pragma: no cover - attempts 至少为 1,这是防御式兜底
            observation = Observation(ok=False, error="tool execution did not produce an observation", elapsed_ms=0)
        observation.metadata.update(
            _observation_metadata(
                context,
                started_at,
                attempts=_metadata_int(context.metadata.get("attempt"), attempts),
                short_circuited=False,
            )
        )
        return self.after_tool(context, observation)


class ToolGovernanceMiddleware:
    """用 ToolSpec 元数据执行权限、审批和预算治理。

    这层把 permissionScope / sideEffect / riskLevel / costUnits 从“展示信息”
    变成真正会影响执行的策略输入。
    """

    def __init__(self, policy: ToolGovernancePolicy | None = None) -> None:
        self.policy = policy or ToolGovernancePolicy()
        self._cost_units_by_run: dict[str, int] = {}

    def before_tool(self, context: ToolCallContext) -> Observation | None:
        allowed_scopes = self._allowed_scopes(context)
        context.metadata["governancePolicy"] = {
            "allowedPermissionScopes": sorted(allowed_scopes) if allowed_scopes is not None else None,
            "deniedPermissionScopes": sorted(self.policy.denied_permission_scopes),
            "requireApprovalForRiskLevels": sorted(self.policy.require_approval_for_risk_levels),
            "requireApprovalForSideEffects": sorted(self.policy.require_approval_for_side_effects),
            "maxCostUnitsPerRun": self.policy.max_cost_units_per_run,
        }

        denied = self._permission_denial(context)
        if denied:
            return Observation(ok=False, elapsed_ms=0, error=denied)

        approval_reason = self._approval_reason(context)
        if approval_reason and context.metadata.get("approved") is not True:
            context.metadata["approvalRequired"] = True
            context.metadata["approvalReason"] = approval_reason
            return Observation(
                ok=False,
                elapsed_ms=0,
                error=f"tool {context.tool.name} requires approval before execution: {approval_reason}",
            )

        budget_denial = self._budget_denial(context)
        if budget_denial:
            return Observation(ok=False, elapsed_ms=0, error=budget_denial)
        return None

    def after_tool(self, context: ToolCallContext, observation: Observation) -> Observation:
        return observation

    def _permission_denial(self, context: ToolCallContext) -> str | None:
        scope = context.tool.permission_scope
        if scope in self.policy.denied_permission_scopes:
            return f"permission scope denied: {scope}"
        allowed_scopes = self.policy.allowed_permission_scopes
        if allowed_scopes is None:
            allowed_scopes = context.identity.permission_scopes
        if allowed_scopes is not None and scope not in allowed_scopes:
            return f"permission scope not allowed: {scope}"
        return None

    def _approval_reason(self, context: ToolCallContext) -> str | None:
        if context.tool.requires_approval:
            return "tool metadata requires approval"
        if context.tool.risk_level in self.policy.require_approval_for_risk_levels:
            return f"risk level {context.tool.risk_level} requires approval"
        if context.tool.side_effect in self.policy.require_approval_for_side_effects:
            return f"side effect {context.tool.side_effect} requires approval"
        return None

    def _budget_denial(self, context: ToolCallContext) -> str | None:
        max_cost = self.policy.max_cost_units_per_run
        if max_cost is None:
            return None
        budget_key = self._budget_key(context)
        current_cost = self._cost_units_by_run.get(budget_key, 0)
        projected_cost = current_cost + context.tool.cost_units
        context.metadata["budgetKey"] = budget_key
        context.metadata["budgetUsedBefore"] = current_cost
        context.metadata["budgetUsedAfter"] = projected_cost
        context.metadata["budgetMaxCostUnits"] = max_cost
        if projected_cost > max_cost:
            return f"tool budget exceeded: {projected_cost}/{max_cost} cost units"
        self._cost_units_by_run[budget_key] = projected_cost
        return None

    def _allowed_scopes(self, context: ToolCallContext) -> set[str] | None:
        if self.policy.allowed_permission_scopes is not None:
            return self.policy.allowed_permission_scopes
        return context.identity.permission_scopes

    def _budget_key(self, context: ToolCallContext) -> str:
        return f"{context.identity.tenant_id}:{context.run_id}"


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


def _execute_with_timeout(execute: Callable[[], Any], timeout_ms: int) -> Any:
    """用标准库线程池实现学习版 timeout。

    Python 线程无法被强制杀死;超时后我们会返回失败 observation,后台线程尽力取消。
    生产中 IO 工具应优先使用底层 HTTP/DB 客户端自己的 timeout。
    """

    timeout_seconds = max(timeout_ms, 1) / 1000
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = executor.submit(execute)
    try:
        return future.result(timeout=timeout_seconds)
    except concurrent.futures.TimeoutError as error:
        future.cancel()
        raise TimeoutError(f"tool timed out after {timeout_ms} ms") from error
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def _idempotency_key(context: ToolCallContext) -> str:
    payload = {
        "runId": context.run_id,
        "step": context.step,
        "tool": context.tool.name,
        "input": context.action.input,
        "version": context.tool.version,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"tool_{digest}"


def _observation_metadata(
    context: ToolCallContext,
    started_at: float,
    attempts: int,
    short_circuited: bool,
) -> dict[str, object]:
    return {
        "toolName": context.tool.name,
        "attempts": attempts,
        "retryCount": context.tool.retry_count,
        "timeoutMs": context.tool.timeout_ms,
        "costUnits": context.metadata.get("costUnits", context.tool.cost_units),
        "permissionScope": context.tool.permission_scope,
        "sideEffect": context.tool.side_effect,
        "riskLevel": context.tool.risk_level,
        "identity": context.identity.to_dict(),
        "requiresApproval": context.tool.requires_approval,
        "approvalRequired": context.metadata.get("approvalRequired", False),
        "approvalReason": context.metadata.get("approvalReason"),
        "budgetUsedBefore": context.metadata.get("budgetUsedBefore"),
        "budgetUsedAfter": context.metadata.get("budgetUsedAfter"),
        "budgetMaxCostUnits": context.metadata.get("budgetMaxCostUnits"),
        "budgetKey": context.metadata.get("budgetKey"),
        "governancePolicy": context.metadata.get("governancePolicy"),
        "idempotencyKey": context.metadata.get("idempotencyKey"),
        "shortCircuited": short_circuited,
        "startedAt": context.metadata.get("startedAt", started_at),
        "elapsedMs": int((time.time() - started_at) * 1000),
    }


def _metadata_int(value: object, default: int) -> int:
    return value if isinstance(value, int) else default
