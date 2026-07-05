"""schemas — Typed State: 全项目共享的数据结构(单一真相源)。

功能:
  - 把在各模块间流转的数据从裸 dict 收敛成 dataclass + Literal 枚举, 让 mypy 能校验、
    让字段有唯一定义。各结构都带 to_dict()(驼峰键), 供 JSON 输出/事件/前端。
  - 决策类: Action(planner 输出) / Observation(工具结果) / MemoryDecision(记忆策略) /
    SafetyDecision(安全策略) / ResponseDecision(见 response_policy)。
  - 记录类: NoteRecord / TodoRecord / MemoryRecord(带 status/importance/expiresAt 生命周期) /
    EventRecord(id/type/runId/payload/source/level/schemaVersion/redacted/payloadSchema)。
  - 运行态: TraceStep(一步 action+observation) / AgentRunState(运行中可变状态) /
    AgentRunResult(一次 run 的完整强类型结果)。
  - 枚举: ActionType / RunStatus / EventType / MemoryType / MemoryStatus。

调用关系图:
  几乎所有模块都 import schemas 的类型(planner/tools/memory/policy/agent/event_*…)——
  它是"数据契约层",本身不依赖其它业务模块(仅引用 runtime_context.RuntimeIdentity)。
  Agent 组装 AgentRunResult ─▶ trace_view 展示 / event_writer 落盘 / eval_harness 统计。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from agentic_core.runtime.context import RuntimeIdentity


# Literal["tool", "final"] 表示 Action.type 只能是这两个字符串之一。
# 这能帮助初学者明确: planner 的输出不是随便写的文本,而是固定结构。
ActionType = Literal["tool", "final"]
RunStatus = Literal["running", "completed", "refused", "clarification", "incomplete", "failed"]
EventType = Literal[
    "run_started",
    "safety_decision",
    "safety_refusal",
    "safety_review_queued",
    "memory_decision",
    "memory_saved",
    "memory_clarification",
    "planner_action",
    "planner_fallback",
    "planner_skipped",
    "tool_started",
    "tool_observation",
    "response_decision",
    "run_completed",
    "run_failed",
    "eval_review_apply",
    "eval_review_apply_failed",
]
MemoryType = Literal["none", "preference", "user_profile", "task_state", "long_term_note"]
MemoryStatus = Literal["active", "archived"]


@dataclass
class Action:
    """Planner 输出的标准动作。

    Agent 每一步只接受两类动作:
        1. tool: 调用某个工具
        2. final: 结束任务并回答用户

    @dataclass 会自动生成 __init__ 等方法,让我们少写样板代码。
    """

    type: ActionType
    reason: str
    tool_name: str | None = None
    input: dict[str, Any] = field(default_factory=dict)
    answer: str | None = None
    source: str = "unknown"

    # metadata 存可观测信息,例如 LLM 原始输出、回退原因。不影响执行逻辑。
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def tool(
        cls,
        tool_name: str,
        input: dict[str, Any] | None = None,
        reason: str = "",
        source: str = "unknown",
    ) -> "Action":
        """创建工具动作的快捷方法。

        @classmethod 表示这个方法属于类本身,可以写 Action.tool(...)。
        """
        return cls(
            type="tool",
            tool_name=tool_name,
            input=input or {},
            reason=reason,
            source=source,
        )

    @classmethod
    def final(cls, answer: str, reason: str = "", source: str = "unknown") -> "Action":
        """创建最终回答动作的快捷方法。"""
        return cls(type="final", answer=answer, reason=reason, source=source)

    def to_dict(self) -> dict[str, Any]:
        """把 dataclass 转成普通 dict,方便打印 JSON 和写入 trace。"""
        data = {
            # 动作类型: "tool" 表示调用工具, "final" 表示直接结束并回答。
            "type": self.type,
            # 规划器为什么选择这个动作,用于学习和调试。
            "reason": self.reason,
            # 工具参数。只有 tool action 真正需要它。
            "input": dict(self.input),
            # 最终回答。只有 final action 真正需要它。
            "answer": self.answer,
            # 动作来源,例如 rule / hermes / rule_fallback。
            "source": self.source,
            # 额外观测信息,例如模型原始输出、回退错误。
            "metadata": dict(self.metadata),
            # 对外 JSON 用 toolName,内部 Python 用 tool_name。
            "toolName": self.tool_name,
        }
        if self.type == "final":
            # final action 不需要 toolName 和 input。
            data.pop("toolName", None)
            data.pop("input", None)
        return data


@dataclass
class Observation:
    """工具执行后的观察结果。

    ok=True 表示工具成功。
    ok=False 表示工具失败,error 里放错误原因。
    """

    ok: bool
    elapsed_ms: int
    output: Any = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            # 工具是否执行成功。False 时通常要看 error。
            "ok": self.ok,
            # 工具耗时,单位是毫秒。
            "elapsed_ms": self.elapsed_ms,
            # 工具成功时的结构化输出。不同工具的输出形状不同。
            "output": self.output,
            # 工具失败时的错误原因。成功时为 None。
            "error": self.error,
            # 工具执行的审计元数据,例如 attempt、timeoutMs、idempotencyKey。
            "metadata": dict(self.metadata),
        }


@dataclass
class MemoryDecision:
    """MemoryPolicy 的输出结果。"""

    save: bool
    memory_type: str
    text: str
    reason: str
    scores: dict[str, int]
    needs_clarification: bool = False
    clarification_question: str | None = None

    # metadata 存可观测信息,例如 LLM 原始输出、来源(llm/rule/rule_fallback)、回退原因。
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            # 是否应该写入长期记忆。
            "save": self.save,
            # 记忆类型,例如 preference / user_profile / none。
            "memory_type": self.memory_type,
            # 真正要保存的规范化文本。save=False 时通常为空。
            "text": self.text,
            # 保存或不保存的原因,用于解释 MemoryPolicy 的判断。
            "reason": self.reason,
            # 各个记忆维度的评分,例如 future_relevance / stability。
            "scores": dict(self.scores),
            # 是否需要向用户追问更多信息。
            "needs_clarification": self.needs_clarification,
            # 追问时展示给用户的问题。
            "clarification_question": self.clarification_question,
            # 额外观测信息,例如 llm 原始输出或 fallback 原因。
            "metadata": dict(self.metadata),
        }


@dataclass
class SafetyDecision:
    """SafetyPolicy 的输出: 是否拒绝整轮请求。

    区别于 MemoryPolicy 的“敏感信息不保存”(local safety),
    这是请求级的全局安全拦截(global safety): 命中即拒绝整轮,不评估记忆、不跑 loop。
    """

    refuse: bool
    category: str  # "none" | "malware" | "weapons" | ...
    reason: str
    risk_level: str = "none"
    confidence: int = 0
    matched_rule: str | None = None
    action: str = "allow"
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            # 是否拒绝整轮请求。True 时 Agent 不再进入 memory/planner/tool。
            "refuse": self.refuse,
            # 命中的安全类别; none 表示未命中。
            "category": self.category,
            # 安全判断原因,用于日志和调试。
            "reason": self.reason,
            # 风险等级: none / low / medium / high。
            "riskLevel": self.risk_level,
            # 规则判断置信度,0-100。
            "confidence": self.confidence,
            # 命中的规则 id。未命中时为 None。
            "matchedRule": self.matched_rule,
            # 系统动作: allow / refuse。
            "action": self.action,
            # 额外审计信息,例如规则说明。
            "metadata": dict(self.metadata),
        }


@dataclass
class NoteRecord:
    """学习笔记的内部结构。

    以前 MemoryStore 直接保存 dict。现在用 dataclass 明确字段,
    让代码知道“一条笔记一定有 id/text/created_at”。
    """

    id: str
    text: str
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            # 笔记唯一 id。
            "id": self.id,
            # 笔记正文。
            "text": self.text,
            # 创建时间。对外 JSON 用 createdAt,内部 Python 用 created_at。
            "createdAt": self.created_at,
        }


@dataclass
class TodoRecord:
    """待办事项的内部结构。"""

    id: str
    text: str
    done: bool
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            # 待办唯一 id。
            "id": self.id,
            # 待办正文。
            "text": self.text,
            # 是否已经完成。
            "done": self.done,
            # 创建时间。对外 JSON 用 createdAt。
            "createdAt": self.created_at,
        }


@dataclass
class MemoryRecord:
    """长期记忆的内部结构。"""

    id: str
    memory_type: str
    text: str
    reason: str
    scores: dict[str, int]
    created_at: str
    updated_at: str | None = None
    last_accessed_at: str | None = None
    access_count: int = 0
    status: MemoryStatus = "active"
    archived_at: str | None = None
    archive_reason: str | None = None
    importance: int = 0
    expires_at: str | None = None
    merged_from: list[str] = field(default_factory=list)
    user_id: str = "local_user"
    tenant_id: str = "default_tenant"

    def to_dict(self) -> dict[str, Any]:
        return {
            # 长期记忆唯一 id。
            "id": self.id,
            # 记忆类型。对外沿用 type,内部用 memory_type 避免和 Python 语义混淆。
            "type": self.memory_type,
            # 长期记忆正文。
            "text": self.text,
            # 为什么保存这条记忆。
            "reason": self.reason,
            # 保存时的 MemoryPolicy 评分快照。
            "scores": dict(self.scores),
            # 创建时间。对外 JSON 用 createdAt。
            "createdAt": self.created_at,
            # 最近一次更新/去重命中的时间。对外 JSON 用 updatedAt。
            "updatedAt": self.updated_at,
            # 最近一次被读取用于规划的时间。对外 JSON 用 lastAccessedAt。
            "lastAccessedAt": self.last_accessed_at,
            # 被读取次数,用于后续记忆排序/清理。
            "accessCount": self.access_count,
            # 生命周期状态: active 会进入 snapshot,archived 不再影响 planner。
            "status": self.status,
            # 归档时间。未归档时为 None。
            "archivedAt": self.archived_at,
            # 归档原因。未归档时为 None。
            "archiveReason": self.archive_reason,
            # 重要性评分,0-100。后续可用于排序、压缩和清理。
            "importance": self.importance,
            # 过期时间。None 表示没有自动过期策略。
            "expiresAt": self.expires_at,
            # 被语义合并进当前记忆的历史文本快照。
            "mergedFrom": list(self.merged_from),
            # 记忆所属用户,用于多用户隔离。
            "userId": self.user_id,
            # 记忆所属租户,用于多租户隔离。
            "tenantId": self.tenant_id,
        }


@dataclass
class EventRecord:
    """运行事件。

    payload 仍允许是 dict[str, Any],因为事件内容会随阶段变化。
    关键是事件本身的外壳固定:
    id/type/run_id/created_at/payload/schema_version/source/level/redacted/payload_schema。
    """

    id: str
    event_type: str
    run_id: str
    payload: dict[str, Any]
    created_at: str
    source: str = "agent"
    level: str = "info"
    schema_version: int = 1
    redacted: bool = False
    payload_schema_version: int = 1
    payload_schema_valid: bool = True
    payload_schema_errors: list[str] = field(default_factory=list)
    payload_schema_migrations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = {
            # 事件唯一 id。
            "id": self.id,
            # 事件类型,例如 memory_decision / tool_observation。
            "type": self.event_type,
            # 事件属于哪一次 Agent 运行。对外 JSON 用 runId。
            "runId": self.run_id,
            # 事件业务载荷。不同事件类型 payload 不同。
            "payload": _to_jsonable(self.payload),
            # 事件创建时间。对外 JSON 用 createdAt。
            "createdAt": self.created_at,
            # 事件 schema 版本,方便未来演进和兼容老日志。
            "schemaVersion": self.schema_version,
            # 事件来源模块,例如 agent / planner / tool / memory / safety / response。
            "source": self.source,
            # 事件级别,例如 info / warn / error。
            "level": self.level,
            # 是否经过敏感信息脱敏。
            "redacted": self.redacted,
        }
        if isinstance(self.payload, dict):
            # 兼容旧 recentEvents 里把业务字段平铺在事件顶层的显示方式。
            data.update(_to_jsonable(self.payload))
        data["payloadSchema"] = {
            # EventRecord 外壳 schema 和 payload schema 分开演进。
            "version": self.payload_schema_version,
            # 当前 payload 是否满足该 event type 的 required 字段。
            "valid": self.payload_schema_valid,
            # 校验错误列表。为空表示通过。
            "errors": list(self.payload_schema_errors),
            # 从旧 payload schema 迁移到当前版本时执行过哪些迁移。
            "migrationsApplied": list(self.payload_schema_migrations),
        }
        return data


@dataclass
class MemorySnapshot:
    """某一刻的记忆快照。

    Planner/Responder 只能读取 snapshot,不能直接拿 MemoryStore 改数据。
    """

    notes: list[NoteRecord] = field(default_factory=list)
    todos: list[TodoRecord] = field(default_factory=list)
    long_term_memories: list[MemoryRecord] = field(default_factory=list)
    recent_events: list[EventRecord] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            # 当前进程内保存的学习笔记列表。
            "notes": [note.to_dict() for note in self.notes],
            # 当前进程内保存的待办列表。
            "todos": [todo.to_dict() for todo in self.todos],
            # 当前进程内保存的长期记忆列表。对外 JSON 用 longTermMemories。
            "longTermMemories": [memory.to_dict() for memory in self.long_term_memories],
            # 最近事件列表,用于观察本轮或近期发生了什么。
            "recentEvents": [event.to_dict() for event in self.recent_events],
        }


@dataclass
class TraceStep:
    """一次 Plan-Act-Observe 的轨迹项。"""

    step: int
    action: Action
    observation: Observation
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            # 第几步 Plan-Act-Observe。
            "step": self.step,
            # Planner 选择的动作。
            "action": self.action.to_dict(),
            # Tool 执行后的观察结果。
            "observation": self.observation.to_dict(),
            # trace step 创建时间。对外 JSON 用 createdAt。
            "createdAt": self.created_at,
        }


@dataclass
class AgentRunState:
    """Agent.run_typed 执行过程中的可变状态。"""

    run_id: str
    goal: str
    status: RunStatus
    started_at: str
    identity: RuntimeIdentity = field(default_factory=RuntimeIdentity)
    step: int = 0
    safety_decision: SafetyDecision | None = None
    memory_decision: MemoryDecision | None = None
    trace: list[TraceStep] = field(default_factory=list)
    saved_memories: list[MemoryRecord] = field(default_factory=list)
    events: list[EventRecord] = field(default_factory=list)


@dataclass
class AgentRunResult:
    """一次 Agent 运行的 typed 结果。

    run_typed() 返回这个对象; run() 再调用 to_dict() 兼容 CLI/Chat。
    """

    run_id: str
    goal: str
    status: RunStatus
    answer: str
    identity: RuntimeIdentity
    safety_decision: SafetyDecision
    memory_decision: MemoryDecision | None
    response_decision: Any
    trace: list[TraceStep]
    memory_snapshot: MemorySnapshot
    events: list[EventRecord]
    started_at: str
    completed_at: str

    def to_dict(self) -> dict[str, Any]:
        memory_decision = self.memory_decision or skipped_memory_decision().to_dict()
        if isinstance(memory_decision, MemoryDecision):
            memory_decision_data = memory_decision.to_dict()
        else:
            memory_decision_data = memory_decision
        return {
            # 本次运行唯一 id。对外 JSON 用 runId。
            "runId": self.run_id,
            # 用户本轮输入的原始目标。
            "goal": self.goal,
            # 本次运行最终状态,例如 completed / refused / clarification。
            "status": self.status,
            # 最终回复给用户的文本。
            "answer": self.answer,
            # 本次运行的身份/租户上下文。
            "identity": self.identity.to_dict(),
            # MemoryPolicy 的判断结果; safety 拦截时用 skipped 占位。
            "memoryDecision": memory_decision_data,
            # SafetyPolicy 的判断结果。
            "safetyDecision": self.safety_decision.to_dict(),
            # ResponsePolicy 的回复仲裁结果。
            "responseDecision": self.response_decision.to_dict(),
            # Plan-Act-Observe 轨迹。
            "trace": [step.to_dict() for step in self.trace],
            # 本轮结束时的记忆快照。
            "memory": self.memory_snapshot.to_dict(),
            # 本轮产生的事件列表。
            "events": [event.to_dict() for event in self.events],
            # 本轮开始时间。对外 JSON 用 startedAt。
            "startedAt": self.started_at,
            # 本轮完成时间。对外 JSON 用 completedAt。
            "completedAt": self.completed_at,
        }


def skipped_memory_decision() -> MemoryDecision:
    """global safety 拦截时,兼容旧 JSON 的“未运行 MemoryPolicy”占位。"""

    return MemoryDecision(
        save=False,
        memory_type="none",
        text="",
        reason="MemoryPolicy skipped because SafetyPolicy refused the request",
        scores={},
        metadata={"source": "skipped_by_safety"},
    )


def _to_jsonable(value: Any) -> Any:
    """把内部 typed object 转成适合 JSON 打印的值。"""

    if hasattr(value, "to_dict"):
        return value.to_dict()
    if isinstance(value, dict):
        return {key: _to_jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_to_jsonable(item) for item in value]
    return value
