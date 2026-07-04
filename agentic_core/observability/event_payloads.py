"""event_payloads — EventRecord.payload 的事件类型级 schema。

EventRecord 的外壳已经稳定:id/type/runId/createdAt/source/level/payload。
这一层负责把不同事件类型的 payload 也收敛起来:

  - Agent 新代码优先使用下面的 dataclass payload,减少裸 dict 扩散。
  - MemoryStore.record_event 写入前会做轻量校验,并把校验结果写到 EventRecord.payloadSchema。
  - 旧 dict 调用仍兼容,但如果缺少该事件类型的关键字段,事件会标记为 payloadSchema.valid=false。

这是标准库学习版 schema,不是 Pydantic/msgspec。它的价值在于先把契约说清楚,
后续迁移到更强的运行时 schema 库时,字段边界已经稳定。

调用关系图:
  Agent(_record_event) ─▶ 构造 *Payload dataclass ─▶ payload_to_dict ─▶ EventRecord.payload
  MemoryStore.record_event ─▶ validate_event_payload(event_type, payload) ─▶ EventPayloadValidation
                              └─▶ 写进 EventRecord.payloadSchema(valid / errors), 兼容旧裸 dict 调用
  下游: event_log / eval_dataset 读取这些结构化 payload。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


PAYLOAD_SCHEMA_VERSION = 1


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
