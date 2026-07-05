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
import contextlib
import copy
import os
import hashlib
import json
import sqlite3
import types
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Iterator
from typing import Any, Callable, Protocol
from urllib.request import Request, urlopen

from agentic_core.policies.memory import SENSITIVE_PATTERN
from agentic_core.runtime.context import RuntimeIdentity
from agentic_core.runtime.schemas import Action, Observation
from agentic_core.tools.registry import RiskLevel, SideEffect, ToolSpec

fcntl: types.ModuleType | None
try:
    import fcntl as _fcntl_module
except ImportError:  # pragma: no cover - 非 POSIX 平台降级无文件锁
    fcntl = None
else:
    fcntl = _fcntl_module


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


@dataclass(frozen=True)
class ToolBudgetDecision:
    """工具预算预留结果。"""

    allowed: bool
    budget_key: str
    used_before: int
    used_after: int
    max_cost_units: int


class ToolBudgetStore(Protocol):
    """工具预算存储协议。

    预算要能跨 middleware 实例共享,否则生产里多个 worker/CLI 进程会各算各的。
    """

    def reserve(self, budget_key: str, cost_units: int, max_cost_units: int) -> ToolBudgetDecision:
        ...


class InMemoryToolBudgetStore:
    """进程内工具预算存储。"""

    def __init__(self) -> None:
        self._cost_units_by_key: dict[str, int] = {}

    def reserve(self, budget_key: str, cost_units: int, max_cost_units: int) -> ToolBudgetDecision:
        current_cost = self._cost_units_by_key.get(budget_key, 0)
        projected_cost = current_cost + cost_units
        allowed = projected_cost <= max_cost_units
        if allowed:
            self._cost_units_by_key[budget_key] = projected_cost
        return ToolBudgetDecision(
            allowed=allowed,
            budget_key=budget_key,
            used_before=current_cost,
            used_after=projected_cost,
            max_cost_units=max_cost_units,
        )


class JsonFileToolBudgetStore:
    """本地 JSON 文件工具预算存储。

    这是学习版跨进程预算后端。它用 append-free 的小 JSON 文件保存每个 budget key
    已使用的 cost units,并用可选文件锁保护读改写。生产里可替换为 Redis/Postgres。
    """

    def __init__(self, path: str | Path = "data/tool-budgets.json", use_lock: bool = True) -> None:
        self.path = Path(path)
        self.use_lock = use_lock
        self.lock_path = self.path.with_name(f"{self.path.name}.lock")

    def reserve(self, budget_key: str, cost_units: int, max_cost_units: int) -> ToolBudgetDecision:
        with self._locked():
            data = self._load()
            budgets = data.setdefault("budgets", {})
            if not isinstance(budgets, dict):
                budgets = {}
                data["budgets"] = budgets
            current_cost = _metadata_int(budgets.get(budget_key), 0)
            projected_cost = current_cost + cost_units
            allowed = projected_cost <= max_cost_units
            if allowed:
                budgets[budget_key] = projected_cost
                self._save(data)
            return ToolBudgetDecision(
                allowed=allowed,
                budget_key=budget_key,
                used_before=current_cost,
                used_after=projected_cost,
                max_cost_units=max_cost_units,
            )

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"schemaVersion": 1, "budgets": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {"schemaVersion": 1, "budgets": {}}
        return data if isinstance(data, dict) else {"schemaVersion": 1, "budgets": {}}

    def _save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schemaVersion": 1,
            "budgets": data.get("budgets", {}),
        }
        temp_path = self.path.with_name(f"{self.path.name}.tmp")
        temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        temp_path.replace(self.path)

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        if not self.use_lock:
            yield
            return
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a", encoding="utf-8") as lock_file:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


class SQLiteToolBudgetStore:
    """SQLite 工具预算存储。

    相比 JSON 文件,SQLite 能用事务保护 reserve 的读改写过程,更接近生产里
    Redis/Postgres 这类共享预算后端的语义。
    """

    def __init__(self, path: str | Path = "data/tool-runtime.db") -> None:
        self.path = Path(path)
        self._ensure_schema()

    def reserve(self, budget_key: str, cost_units: int, max_cost_units: int) -> ToolBudgetDecision:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT used_units FROM tool_budgets WHERE budget_key = ?",
                (budget_key,),
            ).fetchone()
            current_cost = int(row[0]) if row is not None else 0
            projected_cost = current_cost + cost_units
            allowed = projected_cost <= max_cost_units
            if allowed:
                connection.execute(
                    """
                    INSERT INTO tool_budgets (budget_key, used_units, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(budget_key) DO UPDATE SET
                        used_units = excluded.used_units,
                        updated_at = excluded.updated_at
                    """,
                    (budget_key, projected_cost, _iso_now()),
                )
            connection.commit()
        return ToolBudgetDecision(
            allowed=allowed,
            budget_key=budget_key,
            used_before=current_cost,
            used_after=projected_cost,
            max_cost_units=max_cost_units,
        )

    def _ensure_schema(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS tool_budgets (
                    budget_key TEXT PRIMARY KEY,
                    used_units INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=5)


@dataclass(frozen=True)
class ToolTraceSpan:
    """一次工具调用的 OTel-style span。

    这是标准库学习版 tracing 边界,不直接依赖 OpenTelemetry SDK。生产里可以把
    ToolTraceSink 替换成真正的 OTel exporter,字段语义仍保持一致。
    注意: span 不保存工具输入/输出,避免形成第二份敏感数据副本。
    """

    trace_id: str
    span_id: str
    parent_span_id: str | None
    name: str
    run_id: str
    step: int
    tool_name: str
    started_at: str
    ended_at: str
    duration_ms: int
    status: str
    attributes: dict[str, Any]
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """转换成稳定 JSON 字段,方便 JSONL 落盘或未来接 OTel exporter。"""

        return {
            "schemaVersion": 1,
            "traceId": self.trace_id,
            "spanId": self.span_id,
            "parentSpanId": self.parent_span_id,
            "name": self.name,
            "runId": self.run_id,
            "step": self.step,
            "toolName": self.tool_name,
            "startedAt": self.started_at,
            "endedAt": self.ended_at,
            "durationMs": self.duration_ms,
            "status": self.status,
            "attributes": self.attributes,
            "error": self.error,
        }


class ToolTraceSink(Protocol):
    """工具调用 span 写入协议。"""

    def record(self, span: ToolTraceSpan) -> None:
        ...


class InMemoryToolTraceSink:
    """进程内工具 span sink,适合测试和学习。"""

    def __init__(self) -> None:
        self.spans: list[ToolTraceSpan] = []

    def record(self, span: ToolTraceSpan) -> None:
        self.spans.append(span)


class JsonlToolTraceSink:
    """本地 JSONL 工具 span sink。

    每行一个 span JSON,方便 grep/jq/脚本分析。生产里应替换为 OTel collector
    或集中式日志/追踪平台。
    """

    def __init__(self, path: str | Path = "data/tool-spans.jsonl", use_lock: bool = True) -> None:
        self.path = Path(path)
        self.use_lock = use_lock
        self.lock_path = self.path.with_name(f"{self.path.name}.lock")

    def record(self, span: ToolTraceSpan) -> None:
        payload = json.dumps(span.to_dict(), ensure_ascii=False, sort_keys=True)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._locked():
            with self.path.open("a", encoding="utf-8") as file:
                file.write(payload + "\n")

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        if not self.use_lock:
            yield
            return
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a", encoding="utf-8") as lock_file:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


class OtlpHttpToolTraceSink:
    """OTLP/HTTP JSON 工具 span exporter。

    这是直接对接 OpenTelemetry Collector 的标准库版本。它不是官方 SDK,但输出
    `/v1/traces` 常用 JSON 结构,用于把当前 runtime 的工具 span 接到生产可观测平台。
    """

    def __init__(
        self,
        endpoint: str = "http://localhost:4318/v1/traces",
        timeout_ms: int = 1000,
        service_name: str = "agentic-core-lab",
        service_version: str = "0.1.0",
        deployment_environment: str = "local",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.timeout_ms = timeout_ms
        self.service_name = service_name
        self.service_version = service_version
        self.deployment_environment = deployment_environment
        self.headers = headers or {}

    def record(self, span: ToolTraceSpan) -> None:
        payload = _otlp_trace_payload(
            span,
            service_name=self.service_name,
            service_version=self.service_version,
            deployment_environment=self.deployment_environment,
        )
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        request = Request(
            self.endpoint,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                **self.headers,
            },
        )
        with urlopen(request, timeout=max(self.timeout_ms, 1) / 1000) as response:
            status = int(getattr(response, "status", 200))
            if status < 200 or status >= 300:
                raise RuntimeError(f"OTLP exporter returned HTTP {status}")


class ToolMiddleware(Protocol):
    """工具调用中间件协议。

    before_tool 返回 Observation 时表示短路工具执行。
    after_tool 可以查看或改写工具执行结果。
    """

    def before_tool(self, context: ToolCallContext) -> Observation | None:
        ...

    def after_tool(self, context: ToolCallContext, observation: Observation) -> Observation:
        ...


class IdempotencyStore(Protocol):
    """工具幂等结果存储协议。

    幂等不是只生成一个 key,还要能用这个 key 找回第一次成功执行的结果。
    生产环境通常用 Redis/Postgres 等共享存储;学习版先提供内存实现。
    """

    def get(self, key: str) -> Observation | None:
        ...

    def put(self, key: str, observation: Observation) -> None:
        ...


class InMemoryIdempotencyStore:
    """进程内幂等存储。

    只适合同一个 Python 进程内的学习和测试。退出进程后缓存消失,
    多进程 CLI/服务之间也不会共享。读写时都复制 Observation,避免调用方误改缓存。
    """

    def __init__(self) -> None:
        self._observations: dict[str, Observation] = {}

    def get(self, key: str) -> Observation | None:
        observation = self._observations.get(key)
        if observation is None:
            return None
        return _copy_observation(observation)

    def put(self, key: str, observation: Observation) -> None:
        self._observations[key] = _copy_observation(observation)


class JsonFileIdempotencyStore:
    """本地 JSON 文件幂等存储。

    这是学习版跨进程幂等后端。它保存的是已经经过 after_tool 中间件处理后的
    Observation,因此 ToolOutputSafetyMiddleware 净化后的结果才会落盘。
    生产里应替换为 Redis/Postgres,并加 TTL、并发原子性和容量治理。
    """

    def __init__(self, path: str | Path = "data/tool-idempotency.json", use_lock: bool = True) -> None:
        self.path = Path(path)
        self.use_lock = use_lock
        self.lock_path = self.path.with_name(f"{self.path.name}.lock")

    def get(self, key: str) -> Observation | None:
        with self._locked():
            data = self._load()
            observations = data.get("observations", {})
            if not isinstance(observations, dict):
                return None
            item = observations.get(key)
            if not isinstance(item, dict):
                return None
            return _observation_from_dict(item)

    def put(self, key: str, observation: Observation) -> None:
        with self._locked():
            data = self._load()
            observations = data.setdefault("observations", {})
            if not isinstance(observations, dict):
                observations = {}
                data["observations"] = observations
            observations[key] = observation.to_dict()
            self._save(data)

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"schemaVersion": 1, "observations": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {"schemaVersion": 1, "observations": {}}
        return data if isinstance(data, dict) else {"schemaVersion": 1, "observations": {}}

    def _save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schemaVersion": 1,
            "observations": data.get("observations", {}),
        }
        temp_path = self.path.with_name(f"{self.path.name}.tmp")
        temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        temp_path.replace(self.path)

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        if not self.use_lock:
            yield
            return
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a", encoding="utf-8") as lock_file:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


class SQLiteIdempotencyStore:
    """SQLite 工具幂等存储。

    保存已经通过 after_tool 中间件处理后的 Observation JSON,因此敏感输出净化后
    才会落盘。生产里可替换为 Redis/Postgres 并增加 TTL/容量治理。
    """

    def __init__(self, path: str | Path = "data/tool-runtime.db") -> None:
        self.path = Path(path)
        self._ensure_schema()

    def get(self, key: str) -> Observation | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT observation_json FROM tool_idempotency WHERE idempotency_key = ?",
                (key,),
            ).fetchone()
        if row is None:
            return None
        try:
            data = json.loads(str(row[0]))
        except json.JSONDecodeError:
            return None
        return _observation_from_dict(data) if isinstance(data, dict) else None

    def put(self, key: str, observation: Observation) -> None:
        payload = json.dumps(observation.to_dict(), ensure_ascii=False, sort_keys=True)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO tool_idempotency (idempotency_key, observation_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(idempotency_key) DO UPDATE SET
                    observation_json = excluded.observation_json,
                    updated_at = excluded.updated_at
                """,
                (key, payload, _iso_now()),
            )
            connection.commit()

    def _ensure_schema(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS tool_idempotency (
                    idempotency_key TEXT PRIMARY KEY,
                    observation_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=5)


class MiddlewarePipeline:
    """按顺序执行一组工具中间件。"""

    def __init__(
        self,
        middlewares: list[ToolMiddleware] | None = None,
        idempotency_store: IdempotencyStore | None = None,
        trace_sink: ToolTraceSink | None = None,
    ) -> None:
        self.middlewares = middlewares or []
        self.idempotency_store = idempotency_store or InMemoryIdempotencyStore()
        self.trace_sink = trace_sink or InMemoryToolTraceSink()

    @classmethod
    def default(
        cls,
        budget_store: ToolBudgetStore | None = None,
        idempotency_store: IdempotencyStore | None = None,
        trace_sink: ToolTraceSink | None = None,
    ) -> "MiddlewarePipeline":
        """默认管道。

        ApprovalMiddleware 是生产级必备边界,但默认工具都不需要审批,
        所以不会改变现有用户行为。
        CostAccountingMiddleware 只记录成本,不阻断。
        """

        return cls(
            [
                ToolGovernanceMiddleware(budget_store=budget_store),
                CostAccountingMiddleware(),
                ToolOutputSafetyMiddleware(),
            ],
            idempotency_store=idempotency_store,
            trace_sink=trace_sink,
        )

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
        context.metadata.setdefault("idempotencyHit", False)
        context.metadata.setdefault("idempotencyStored", False)
        context.metadata.setdefault("idempotencyStore", self.idempotency_store.__class__.__name__)
        context.metadata.setdefault("traceSink", self.trace_sink.__class__.__name__)

        short_circuit = self.before_tool(context)
        if short_circuit is not None:
            short_circuit.metadata.update(_observation_metadata(context, started_at, attempts=0, short_circuited=True))
            final_observation = self.after_tool(context, short_circuit)
            self._record_tool_span(context, final_observation, started_at)
            return final_observation

        idempotency_key = str(context.metadata["idempotencyKey"])
        should_use_idempotency = context.tool.side_effect == "write"
        if should_use_idempotency:
            cached = self.idempotency_store.get(idempotency_key)
            if cached is not None:
                context.metadata["idempotencyHit"] = True
                cached.metadata.update(
                    _observation_metadata(
                        context,
                        started_at,
                        attempts=0,
                        short_circuited=True,
                    )
                )
                cached_observation = self.after_tool(context, cached)
                self._record_tool_span(context, cached_observation, started_at)
                return cached_observation

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
        observation = self.after_tool(context, observation)
        if should_use_idempotency and observation.ok:
            context.metadata["idempotencyStored"] = True
            observation.metadata["idempotencyStored"] = True
            self.idempotency_store.put(idempotency_key, observation)
        self._record_tool_span(context, observation, started_at)
        return observation

    def _record_tool_span(self, context: ToolCallContext, observation: Observation, started_at: float) -> None:
        """记录工具调用 span。

        tracing 是可观测边界,不能影响用户主流程;所以 sink 失败只写入 metadata,
        不把工具执行改成失败。
        """

        ended_at = time.time()
        span_id = _tool_span_id(context, started_at)
        observation.metadata["toolTraceSpanId"] = span_id
        observation.metadata["traceSink"] = self.trace_sink.__class__.__name__
        span = ToolTraceSpan(
            trace_id=context.run_id,
            span_id=span_id,
            parent_span_id=_metadata_str(context.metadata.get("parentSpanId")),
            name=f"tool.{context.tool.name}",
            run_id=context.run_id,
            step=context.step,
            tool_name=context.tool.name,
            started_at=_iso_from_epoch(started_at),
            ended_at=_iso_from_epoch(ended_at),
            duration_ms=int((ended_at - started_at) * 1000),
            status="ok" if observation.ok else "error",
            attributes=_tool_span_attributes(context, observation),
            error=_redacted_error(observation.error),
        )
        try:
            self.trace_sink.record(span)
        except Exception as error:  # pragma: no cover - 外部 tracing 后端失败不能拖垮工具主流程
            observation.metadata["toolTraceError"] = str(error)


class ToolGovernanceMiddleware:
    """用 ToolSpec 元数据执行权限、审批和预算治理。

    这层把 permissionScope / sideEffect / riskLevel / costUnits 从“展示信息”
    变成真正会影响执行的策略输入。
    """

    def __init__(
        self,
        policy: ToolGovernancePolicy | None = None,
        budget_store: ToolBudgetStore | None = None,
    ) -> None:
        self.policy = policy or ToolGovernancePolicy()
        self.budget_store = budget_store or InMemoryToolBudgetStore()

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
        budget_decision = self.budget_store.reserve(
            budget_key,
            context.tool.cost_units,
            max_cost,
        )
        context.metadata["budgetKey"] = budget_key
        context.metadata["budgetUsedBefore"] = budget_decision.used_before
        context.metadata["budgetUsedAfter"] = budget_decision.used_after
        context.metadata["budgetMaxCostUnits"] = max_cost
        context.metadata["budgetStore"] = self.budget_store.__class__.__name__
        if not budget_decision.allowed:
            return f"tool budget exceeded: {budget_decision.used_after}/{max_cost} cost units"
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


class ToolOutputSafetyMiddleware:
    """净化工具输出和错误,避免敏感信息进入回复、trace 或事件。

    ToolRegistry.guard_sensitive 负责执行前拦截敏感输入;这一层负责执行后兜底。
    生产里可以替换成更强的 DLP/内容安全服务,学习版先复用 SENSITIVE_PATTERN。
    """

    def before_tool(self, context: ToolCallContext) -> Observation | None:
        return None

    def after_tool(self, context: ToolCallContext, observation: Observation) -> Observation:
        redacted_fields: list[str] = []
        output = _redact_sensitive_value(observation.output, "output", redacted_fields)
        error = _redact_sensitive_string(observation.error, "error", redacted_fields)
        if not redacted_fields:
            observation.metadata.setdefault(
                "toolOutputSafety",
                {"redacted": False, "fields": [], "source": "ToolOutputSafetyMiddleware"},
            )
            return observation
        return Observation(
            ok=observation.ok,
            output=output,
            error=error,
            elapsed_ms=observation.elapsed_ms,
            metadata={
                **observation.metadata,
                "toolOutputSafety": {
                    "redacted": True,
                    "fields": redacted_fields,
                    "source": "ToolOutputSafetyMiddleware",
                },
            },
        )


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
        "userId": context.identity.user_id,
        "tenantId": context.identity.tenant_id,
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
        "budgetStore": context.metadata.get("budgetStore"),
        "governancePolicy": context.metadata.get("governancePolicy"),
        "idempotencyKey": context.metadata.get("idempotencyKey"),
        "idempotencyHit": context.metadata.get("idempotencyHit", False),
        "idempotencyStored": context.metadata.get("idempotencyStored", False),
        "idempotencyStore": context.metadata.get("idempotencyStore"),
        "shortCircuited": short_circuited,
        "startedAt": context.metadata.get("startedAt", started_at),
        "elapsedMs": int((time.time() - started_at) * 1000),
    }


def _tool_span_id(context: ToolCallContext, started_at: float) -> str:
    raw = f"{context.run_id}:{context.step}:{context.tool.name}:{started_at:.9f}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"span_{digest}"


def _tool_span_attributes(context: ToolCallContext, observation: Observation) -> dict[str, Any]:
    """生成 span 属性白名单。

    这里故意不写 action.input / observation.output,span 只承担治理和性能观测职责。
    """

    identity = context.identity.to_dict()
    attributes: dict[str, Any] = {
        "toolName": context.tool.name,
        "toolVersion": context.tool.version,
        "sideEffect": context.tool.side_effect,
        "riskLevel": context.tool.risk_level,
        "permissionScope": context.tool.permission_scope,
        "requiresApproval": context.tool.requires_approval,
        "approvalRequired": observation.metadata.get("approvalRequired", False),
        "approvalReason": observation.metadata.get("approvalReason"),
        "timeoutMs": context.tool.timeout_ms,
        "retryCount": context.tool.retry_count,
        "attempts": observation.metadata.get("attempts"),
        "costUnits": observation.metadata.get("costUnits", context.tool.cost_units),
        "budgetKey": observation.metadata.get("budgetKey"),
        "budgetUsedBefore": observation.metadata.get("budgetUsedBefore"),
        "budgetUsedAfter": observation.metadata.get("budgetUsedAfter"),
        "budgetMaxCostUnits": observation.metadata.get("budgetMaxCostUnits"),
        "budgetStore": observation.metadata.get("budgetStore"),
        "idempotencyKey": observation.metadata.get("idempotencyKey"),
        "idempotencyHit": observation.metadata.get("idempotencyHit", False),
        "idempotencyStored": observation.metadata.get("idempotencyStored", False),
        "idempotencyStore": observation.metadata.get("idempotencyStore"),
        "shortCircuited": observation.metadata.get("shortCircuited", False),
        "toolOutputRedacted": _tool_output_redacted(observation),
        "owner": context.tool.owner,
        "slaTier": context.tool.sla_tier,
        "dataClassification": context.tool.data_classification,
        "auditClassification": context.tool.audit_classification,
        "externalSideEffect": context.tool.external_side_effect,
        "lifecycleStatus": context.tool.lifecycle_status,
        "userId": identity.get("userId"),
        "tenantId": identity.get("tenantId"),
        "roles": identity.get("roles"),
    }
    return _json_safe(attributes)


def _tool_output_redacted(observation: Observation) -> bool:
    safety = observation.metadata.get("toolOutputSafety")
    if not isinstance(safety, dict):
        return False
    return safety.get("redacted") is True


def _redacted_error(error: str | None) -> str | None:
    return _redact_sensitive_string(error, "error", [])


def _metadata_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _iso_from_epoch(value: float) -> str:
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()


def _iso_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return str(value)


def _otlp_trace_payload(
    span: ToolTraceSpan,
    service_name: str,
    service_version: str,
    deployment_environment: str,
) -> dict[str, Any]:
    attributes = {
        **span.attributes,
        "runId": span.run_id,
        "toolName": span.tool_name,
        "step": span.step,
        "durationMs": span.duration_ms,
    }
    if span.error:
        attributes["error.message"] = span.error
    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        _otlp_attribute("service.name", service_name),
                        _otlp_attribute("service.version", service_version),
                        _otlp_attribute("deployment.environment", deployment_environment),
                    ]
                },
                "scopeSpans": [
                    {
                        "scope": {"name": "agentic_core.tools.middleware", "version": "1.0"},
                        "spans": [
                            {
                                "traceId": _otlp_trace_id(span.trace_id),
                                "spanId": _otlp_span_id(span.span_id),
                                "parentSpanId": _otlp_span_id(span.parent_span_id)
                                if span.parent_span_id
                                else "",
                                "name": span.name,
                                "kind": "SPAN_KIND_INTERNAL",
                                "startTimeUnixNano": str(_iso_to_unix_nano(span.started_at)),
                                "endTimeUnixNano": str(_iso_to_unix_nano(span.ended_at)),
                                "attributes": [
                                    _otlp_attribute(key, value)
                                    for key, value in sorted(attributes.items())
                                    if value is not None
                                ],
                                "status": {
                                    "code": "STATUS_CODE_OK"
                                    if span.status == "ok"
                                    else "STATUS_CODE_ERROR",
                                    "message": span.error or "",
                                },
                            }
                        ],
                    }
                ],
            }
        ]
    }


def _otlp_attribute(key: str, value: Any) -> dict[str, Any]:
    return {"key": key, "value": _otlp_any_value(value)}


def _otlp_any_value(value: Any) -> dict[str, Any]:
    value = _json_safe(value)
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, list):
        return {"arrayValue": {"values": [_otlp_any_value(item) for item in value]}}
    if isinstance(value, dict):
        return {
            "kvlistValue": {
                "values": [
                    _otlp_attribute(key, item)
                    for key, item in sorted(value.items())
                ]
            }
        }
    return {"stringValue": "" if value is None else str(value)}


def _otlp_trace_id(value: str) -> str:
    cleaned = "".join(char for char in value.lower() if char in "0123456789abcdef")
    if len(cleaned) >= 32:
        return cleaned[:32]
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


def _otlp_span_id(value: str) -> str:
    cleaned = "".join(char for char in value.lower() if char in "0123456789abcdef")
    if len(cleaned) >= 16:
        return cleaned[:16]
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _iso_to_unix_nano(value: str) -> int:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1_000_000_000)


def _metadata_int(value: object, default: int) -> int:
    return value if isinstance(value, int) else default


def _redact_sensitive_value(value: Any, path: str, redacted_fields: list[str]) -> Any:
    if isinstance(value, str):
        return _redact_sensitive_string(value, path, redacted_fields)
    if isinstance(value, dict):
        return {
            key: _redact_sensitive_value(item, f"{path}.{key}", redacted_fields)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            _redact_sensitive_value(item, f"{path}[{index}]", redacted_fields)
            for index, item in enumerate(value)
        ]
    if isinstance(value, tuple):
        return tuple(
            _redact_sensitive_value(item, f"{path}[{index}]", redacted_fields)
            for index, item in enumerate(value)
        )
    return value


def _redact_sensitive_string(value: str | None, path: str, redacted_fields: list[str]) -> str | None:
    if value is None:
        return None
    if not (SENSITIVE_PATTERN.search(path) or SENSITIVE_PATTERN.search(value)):
        return value
    redacted_fields.append(path)
    return "[REDACTED]"


def _copy_observation(observation: Observation) -> Observation:
    """复制 Observation,避免幂等缓存和调用方共享同一个可变对象。"""

    return Observation(
        ok=observation.ok,
        elapsed_ms=observation.elapsed_ms,
        output=copy.deepcopy(observation.output),
        error=observation.error,
        metadata=copy.deepcopy(observation.metadata),
    )


def _observation_from_dict(data: dict[str, Any]) -> Observation:
    metadata = data.get("metadata")
    return Observation(
        ok=bool(data.get("ok", False)),
        elapsed_ms=_metadata_int(data.get("elapsed_ms") or data.get("elapsedMs"), 0),
        output=copy.deepcopy(data.get("output")),
        error=str(data["error"]) if data.get("error") is not None else None,
        metadata=copy.deepcopy(metadata) if isinstance(metadata, dict) else {},
    )


def build_tool_budget_store_from_env() -> ToolBudgetStore:
    """按环境变量创建工具预算存储。

    可选:
        AGENTIC_TOOL_BUDGET_STORE=memory  默认,进程内预算。
        AGENTIC_TOOL_BUDGET_STORE=json    本地 JSON 文件预算。
        AGENTIC_TOOL_BUDGET_STORE=sqlite  本地 SQLite 事务预算。
        AGENTIC_TOOL_BUDGET_PATH=data/tool-budgets.json
        AGENTIC_TOOL_BUDGET_LOCK=0        禁用 JSON 文件锁。
    """

    mode = os.getenv("AGENTIC_TOOL_BUDGET_STORE", "memory").strip().lower()
    path = os.getenv("AGENTIC_TOOL_BUDGET_PATH")
    use_lock = _env_bool(os.getenv("AGENTIC_TOOL_BUDGET_LOCK"), default=True)
    if mode in {"sqlite", "sqlite3"}:
        return SQLiteToolBudgetStore(path or "data/tool-runtime.db")
    if mode in {"json", "file"} or path:
        return JsonFileToolBudgetStore(path or "data/tool-budgets.json", use_lock=use_lock)
    return InMemoryToolBudgetStore()


def build_idempotency_store_from_env() -> IdempotencyStore:
    """按环境变量创建工具幂等存储。

    可选:
        AGENTIC_IDEMPOTENCY_STORE=memory  默认,进程内幂等。
        AGENTIC_IDEMPOTENCY_STORE=json    本地 JSON 文件幂等。
        AGENTIC_IDEMPOTENCY_STORE=sqlite  本地 SQLite 幂等。
        AGENTIC_IDEMPOTENCY_PATH=data/tool-idempotency.json
        AGENTIC_IDEMPOTENCY_LOCK=0        禁用 JSON 文件锁。
    """

    mode = os.getenv("AGENTIC_IDEMPOTENCY_STORE", "memory").strip().lower()
    path = os.getenv("AGENTIC_IDEMPOTENCY_PATH")
    use_lock = _env_bool(os.getenv("AGENTIC_IDEMPOTENCY_LOCK"), default=True)
    if mode in {"sqlite", "sqlite3"}:
        return SQLiteIdempotencyStore(path or "data/tool-runtime.db")
    if mode in {"json", "file"} or path:
        return JsonFileIdempotencyStore(path or "data/tool-idempotency.json", use_lock=use_lock)
    return InMemoryIdempotencyStore()


def build_tool_trace_sink_from_env() -> ToolTraceSink:
    """按环境变量创建工具 tracing sink。

    可选:
        AGENTIC_TOOL_TRACE_SINK=memory  默认,进程内 span。
        AGENTIC_TOOL_TRACE_SINK=jsonl   本地 JSONL span。
        AGENTIC_TOOL_TRACE_SINK=otlp_http  OTLP/HTTP JSON exporter。
        AGENTIC_TOOL_TRACE_PATH=data/tool-spans.jsonl
        AGENTIC_TOOL_TRACE_LOCK=0       禁用 JSONL 文件锁。
        AGENTIC_TOOL_TRACE_ENDPOINT=http://localhost:4318/v1/traces
        AGENTIC_TOOL_TRACE_TIMEOUT_MS=1000
        AGENTIC_SERVICE_NAME=agentic-core-lab
        AGENTIC_SERVICE_VERSION=0.1.0
        AGENTIC_DEPLOYMENT_ENVIRONMENT=local
        AGENTIC_TOOL_TRACE_HEADERS='{"Authorization":"Bearer ..."}'
    """

    mode = os.getenv("AGENTIC_TOOL_TRACE_SINK", "memory").strip().lower()
    path = os.getenv("AGENTIC_TOOL_TRACE_PATH")
    use_lock = _env_bool(os.getenv("AGENTIC_TOOL_TRACE_LOCK"), default=True)
    if mode in {"otlp", "otlp_http", "otel", "opentelemetry"}:
        return OtlpHttpToolTraceSink(
            endpoint=os.getenv("AGENTIC_TOOL_TRACE_ENDPOINT", "http://localhost:4318/v1/traces"),
            timeout_ms=_env_int(os.getenv("AGENTIC_TOOL_TRACE_TIMEOUT_MS"), 1000),
            service_name=os.getenv("AGENTIC_SERVICE_NAME", "agentic-core-lab"),
            service_version=os.getenv("AGENTIC_SERVICE_VERSION", "0.1.0"),
            deployment_environment=os.getenv("AGENTIC_DEPLOYMENT_ENVIRONMENT", "local"),
            headers=_env_json_headers(os.getenv("AGENTIC_TOOL_TRACE_HEADERS")),
        )
    if mode in {"jsonl", "json", "file"} or path:
        return JsonlToolTraceSink(path or "data/tool-spans.jsonl", use_lock=use_lock)
    return InMemoryToolTraceSink()


def build_middleware_pipeline_from_env() -> MiddlewarePipeline:
    """按环境变量创建默认 middleware pipeline。"""

    return MiddlewarePipeline.default(
        budget_store=build_tool_budget_store_from_env(),
        idempotency_store=build_idempotency_store_from_env(),
        trace_sink=build_tool_trace_sink_from_env(),
    )


def _env_bool(value: str | None, default: bool) -> bool:
    if value is None or not value.strip():
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def _env_int(value: str | None, default: int) -> int:
    if value is None or not value.strip():
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _env_json_headers(value: str | None) -> dict[str, str]:
    if value is None or not value.strip():
        return {}
    try:
        data = json.loads(value)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(key): str(item) for key, item in data.items()}
