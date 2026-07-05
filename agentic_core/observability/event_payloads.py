"""event_payloads — EventRecord.payload 的事件类型级 schema。

EventRecord 的外壳已经稳定:id/type/runId/createdAt/source/level/payload。
这一层负责把不同事件类型的 payload 也收敛起来:

  - Agent 新代码优先使用下面的 dataclass payload,减少裸 dict 扩散。
  - MemoryStore.record_event 写入前会先做 schema migration,再做轻量校验,
    并把校验结果写到 EventRecord.payloadSchema。
  - 旧 dict 调用仍兼容,但如果缺少该事件类型的关键字段,事件会标记为 payloadSchema.valid=false。

这是标准库学习版 schema,不是 Pydantic/msgspec。它的价值在于先把契约说清楚,
后续迁移到更强的运行时 schema 库时,字段边界已经稳定。

调用关系图:
  Agent(_record_event) ─▶ 构造 *Payload dataclass ─▶ payload_to_dict ─▶ EventRecord.payload
  MemoryStore.record_event ─▶ migrate_event_payload(...) ─▶ validate_event_payload(...)
                              └─▶ 写进 EventRecord.payloadSchema(valid / errors / migrationsApplied)
                                  兼容旧裸 dict 调用
  下游: event_log / eval_dataset 读取这些结构化 payload。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


PAYLOAD_SCHEMA_VERSION = 2


class EventPayload(Protocol):
    """所有 typed event payload 都实现 to_dict。"""

    def to_dict(self) -> dict[str, Any]:
        """转成 EventRecord.payload 使用的 JSON-like dict。"""
        ...


EventPayloadInput = EventPayload | dict[str, Any]


@dataclass(frozen=True)
class EventPayloadSpec:
    """某个 event type 的 payload 形状说明。"""

    required: tuple[str, ...]
    optional: tuple[str, ...] = ()


@dataclass(frozen=True)
class EventPayloadMigrationResult:
    """payload schema migration 的结果。

    生产系统里事件日志是 append-only 的,旧事件不能随便改文件。
    所以读取或重新写入旧事件时,需要一层可重复执行的 migration,把旧 payload
    规范化成当前 schema 再校验和分析。
    """

    payload: dict[str, Any]
    source_schema_version: int
    target_schema_version: int
    migrations_applied: tuple[str, ...] = ()


@dataclass(frozen=True)
class EventPayloadValidation:
    """payload 校验结果,会进入 EventRecord.payloadSchema。"""

    valid: bool
    schema_version: int
    errors: tuple[str, ...] = ()
    missing_required: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.schema_version,
            "valid": self.valid,
            "errors": list(self.errors),
            "missingRequired": list(self.missing_required),
        }


EVENT_PAYLOAD_SPECS: dict[str, EventPayloadSpec] = {
    "run_started": EventPayloadSpec(required=("goal", "identity")),
    "safety_decision": EventPayloadSpec(required=("safety",)),
    "safety_refusal": EventPayloadSpec(required=("safety", "answer")),
    "safety_review_queued": EventPayloadSpec(required=("reviewItem", "safety")),
    "memory_decision": EventPayloadSpec(required=("decision", "savedMemory")),
    "memory_saved": EventPayloadSpec(required=("savedMemory",)),
    "memory_clarification": EventPayloadSpec(required=("decision", "responseDecision", "answer")),
    "planner_action": EventPayloadSpec(required=("step", "action")),
    "planner_fallback": EventPayloadSpec(required=("step", "action", "reason", "metadata")),
    "planner_skipped": EventPayloadSpec(required=("reason", "responseDecision")),
    "tool_started": EventPayloadSpec(required=("step", "action")),
    "tool_observation": EventPayloadSpec(required=("step", "action", "observation")),
    "response_decision": EventPayloadSpec(
        required=("answer", "responseDecision"),
        optional=("incompleteReason",),
    ),
    "run_completed": EventPayloadSpec(required=("status", "answer", "identity")),
    "run_failed": EventPayloadSpec(required=("error", "errorType", "goal", "identity")),
    "event_writer_warning": EventPayloadSpec(required=("failedEventId", "writer", "error")),
    "invalid_jsonl_line": EventPayloadSpec(required=("file", "lineNumber", "error")),
    "eval_review_apply": EventPayloadSpec(
        required=("input", "output", "reviewer", "approve", "reject", "reviewSummary")
    ),
    "eval_review_apply_failed": EventPayloadSpec(required=("input", "output", "error", "errorType")),
}


@dataclass(frozen=True)
class RunStartedPayload:
    goal: str
    identity: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"goal": self.goal, "identity": self.identity}


@dataclass(frozen=True)
class SafetyDecisionPayload:
    safety: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"safety": self.safety}


@dataclass(frozen=True)
class SafetyRefusalPayload:
    safety: dict[str, Any]
    answer: str

    def to_dict(self) -> dict[str, Any]:
        return {"safety": self.safety, "answer": self.answer}


@dataclass(frozen=True)
class SafetyReviewQueuedPayload:
    review_item: dict[str, Any]
    safety: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"reviewItem": self.review_item, "safety": self.safety}


@dataclass(frozen=True)
class MemoryDecisionPayload:
    decision: dict[str, Any]
    saved_memory: dict[str, Any] | None

    def to_dict(self) -> dict[str, Any]:
        return {"decision": self.decision, "savedMemory": self.saved_memory}


@dataclass(frozen=True)
class MemorySavedPayload:
    saved_memory: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"savedMemory": self.saved_memory}


@dataclass(frozen=True)
class MemoryClarificationPayload:
    decision: dict[str, Any]
    response_decision: dict[str, Any]
    answer: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "responseDecision": self.response_decision,
            "answer": self.answer,
        }


@dataclass(frozen=True)
class PlannerActionPayload:
    step: int
    action: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"step": self.step, "action": self.action}


@dataclass(frozen=True)
class PlannerFallbackPayload:
    step: int
    action: dict[str, Any]
    reason: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "action": self.action,
            "reason": self.reason,
            "metadata": self.metadata,
        }


@dataclass(frozen=True)
class PlannerSkippedPayload:
    reason: str
    response_decision: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"reason": self.reason, "responseDecision": self.response_decision}


@dataclass(frozen=True)
class ToolStartedPayload:
    step: int
    action: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"step": self.step, "action": self.action}


@dataclass(frozen=True)
class ToolObservationPayload:
    step: int
    action: dict[str, Any]
    observation: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "action": self.action,
            "observation": self.observation,
        }


@dataclass(frozen=True)
class ResponseDecisionPayload:
    answer: str
    response_decision: dict[str, Any]
    incomplete_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = {"answer": self.answer, "responseDecision": self.response_decision}
        if self.incomplete_reason is not None:
            data["incompleteReason"] = self.incomplete_reason
        return data


@dataclass(frozen=True)
class RunCompletedPayload:
    status: str
    answer: str
    identity: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "answer": self.answer, "identity": self.identity}


@dataclass(frozen=True)
class RunFailedPayload:
    error: str
    error_type: str
    goal: str
    identity: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": self.error,
            "errorType": self.error_type,
            "goal": self.goal,
            "identity": self.identity,
        }


def payload_to_dict(payload: EventPayloadInput | None) -> dict[str, Any]:
    """把 typed payload 或旧 dict 统一转成 JSON-like dict。"""

    if payload is None:
        return {}
    if isinstance(payload, dict):
        return dict(payload)
    return payload.to_dict()


def migrate_event_payload(
    event_type: str,
    payload: dict[str, Any],
    source_schema_version: int | None = None,
) -> EventPayloadMigrationResult:
    """把旧事件 payload 迁移到当前 payload schema。

    规则:
        - source_schema_version 明确小于当前版本时,按版本迁移。
        - 没有版本信息时,只迁移能明确识别的旧扁平格式。
        - 当前新写入的 payload 缺字段时不自动编造,仍交给 validate 标 invalid。

    这样既能兼容历史日志,又不会掩盖新 call site 的结构问题。
    """

    migrated = dict(payload)
    source_version = _normalize_schema_version(source_schema_version)
    if source_version is None:
        source_version = _infer_legacy_payload_schema_version(event_type, migrated)

    migrations: list[str] = []
    if source_version < 1:
        migrated, applied = _migrate_legacy_flat_payload(event_type, migrated)
        migrations.extend(applied)
    if source_version < 2:
        migrated, applied = _migrate_v1_to_v2(event_type, migrated)
        migrations.extend(applied)

    return EventPayloadMigrationResult(
        payload=migrated,
        source_schema_version=source_version,
        target_schema_version=PAYLOAD_SCHEMA_VERSION,
        migrations_applied=tuple(migrations),
    )


def validate_event_payload(event_type: str, payload: dict[str, Any]) -> EventPayloadValidation:
    """按事件类型校验 payload 关键字段。

    当前先校验 required keys,不禁止额外字段。原因是项目仍需兼容旧日志、调试字段和
    后续 schema 扩展。真正强约束可以在下一阶段切到 Pydantic/msgspec。
    """

    spec = EVENT_PAYLOAD_SPECS.get(event_type)
    if spec is None:
        return EventPayloadValidation(
            valid=False,
            schema_version=PAYLOAD_SCHEMA_VERSION,
            errors=(f"unknown event payload schema: {event_type}",),
        )
    missing = tuple(key for key in spec.required if key not in payload)
    errors = tuple(f"missing required payload field: {key}" for key in missing)
    return EventPayloadValidation(
        valid=not missing,
        schema_version=PAYLOAD_SCHEMA_VERSION,
        errors=errors,
        missing_required=missing,
    )


def _normalize_schema_version(value: int | None) -> int | None:
    if value is None:
        return None
    return max(0, min(value, PAYLOAD_SCHEMA_VERSION))


def _infer_legacy_payload_schema_version(event_type: str, payload: dict[str, Any]) -> int:
    """识别没有 payloadSchema.version 的旧扁平事件。

    只有非常明确的旧形态才返回 0。普通 dict 默认视为当前 schema,
    让 validate_event_payload 暴露缺字段。
    """

    if event_type == "tool_observation" and "observation" not in payload:
        if {"ok", "output", "error", "elapsed_ms", "elapsedMs", "metadata"} & set(payload):
            return 0
    if event_type == "planner_action" and "action" not in payload:
        if {"type", "toolName", "input", "answer", "reason"} & set(payload):
            return 0
    return PAYLOAD_SCHEMA_VERSION


def _migrate_legacy_flat_payload(event_type: str, payload: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """v0 → v1: 把早期"扁平" payload 收进 action/observation 嵌套结构。

    早期 tool_observation/planner_action 事件是把 ok/output/toolName 等字段平铺在 payload 顶层的;
    v1 起统一收进 {"action": {...}, "observation": {...}}。这里按事件类型重组,
    并返回一条 migration 标记(供审计"这条事件被迁移过")。识别不了就原样返回。
    """
    if event_type == "tool_observation" and "observation" not in payload:
        step = payload.get("step", 0)
        action = payload.get("action")
        if not isinstance(action, dict):
            action = {
                "type": "tool",
                "toolName": payload.get("toolName"),
                "input": payload.get("input", {}),
                "reason": payload.get("reason", "migrated legacy tool event"),
                "source": payload.get("actionSource", "legacy"),
            }
        observation = {
            "ok": bool(payload.get("ok", False)),
            "output": payload.get("output"),
            "error": payload.get("error"),
            "elapsed_ms": payload.get("elapsed_ms", payload.get("elapsedMs", 0)),
            "metadata": payload.get("metadata", {}),
        }
        return (
            {
                "step": step,
                "action": action,
                "observation": observation,
            },
            ["legacy_flat_tool_observation_to_v1"],
        )
    if event_type == "planner_action" and "action" not in payload:
        action = {
            "type": payload.get("type", "final" if payload.get("answer") else "tool"),
            "toolName": payload.get("toolName"),
            "input": payload.get("input", {}),
            "answer": payload.get("answer"),
            "reason": payload.get("reason", "migrated legacy planner event"),
            "source": payload.get("source", "legacy"),
            "metadata": payload.get("metadata", {}),
        }
        return (
            {
                "step": payload.get("step", 0),
                "action": action,
            },
            ["legacy_flat_planner_action_to_v1"],
        )
    return payload, []


def _migrate_v1_to_v2(event_type: str, payload: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """v1 → v2: 给缺失的新字段补一个"未知/占位"值,而不是让旧事件缺字段。

    v2 起 run_* 事件带 identity、memory_decision 带 savedMemory。老日志没有这些字段,
    读取时补一个明确的占位(identity=unknown / savedMemory=None),既能通过校验又不假装有真实数据。
    """
    migrated = dict(payload)
    migrations: list[str] = []
    if event_type in {"run_started", "run_completed", "run_failed"} and "identity" not in migrated:
        migrated["identity"] = _unknown_identity()
        migrations.append(f"{event_type}.v1.add_unknown_identity")
    if event_type == "memory_decision" and "savedMemory" not in migrated:
        migrated["savedMemory"] = None
        migrations.append("memory_decision.v1.add_saved_memory_none")
    return migrated, migrations


def _unknown_identity() -> dict[str, Any]:
    return {
        "userId": "unknown",
        "tenantId": "unknown",
        "roles": [],
        "permissionScopes": None,
    }
