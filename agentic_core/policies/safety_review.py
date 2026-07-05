"""safety_review — SafetyPolicy 的本地人审队列边界。

功能:
  - SafetyPolicy 只负责判断 allow/warn/review/refuse。
  - 当 action=review 时,Agent 把请求写入 SafetyReviewQueue,供后续人工审核。
  - 默认 InMemorySafetyReviewQueue 保持学习版轻量;JsonlSafetyReviewQueue 可用环境变量开启本地持久化。

调用关系图:
  Agent(SafetyDecision.action == "review")
      └─▶ SafetyReviewQueue.enqueue(SafetyReviewItem)
            ├─ InMemorySafetyReviewQueue
            └─ JsonlSafetyReviewQueue(data/safety-review-queue.jsonl)
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from agentic_core.memory.store import now_iso


SafetyReviewStatus = Literal["pending", "approved", "rejected"]


@dataclass
class SafetyReviewItem:
    """一条待人工审核的安全请求。"""

    id: str
    run_id: str
    goal: str
    safety_decision: dict[str, Any]
    identity: dict[str, Any]
    created_at: str
    status: SafetyReviewStatus = "pending"
    reviewer: str | None = None
    reviewed_at: str | None = None
    notes: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "runId": self.run_id,
            "goal": self.goal,
            "safetyDecision": dict(self.safety_decision),
            "identity": dict(self.identity),
            "createdAt": self.created_at,
            "status": self.status,
            "reviewer": self.reviewer,
            "reviewedAt": self.reviewed_at,
            "notes": self.notes,
        }


class SafetyReviewQueue(Protocol):
    """Safety review queue 协议。

    生产环境可以替换成数据库、工单系统或人工审核平台。
    """

    def enqueue(self, item: SafetyReviewItem) -> SafetyReviewItem:
        ...

    def list_pending(self) -> list[SafetyReviewItem]:
        ...


class InMemorySafetyReviewQueue:
    """进程内 safety review queue。"""

    def __init__(self) -> None:
        self.items: list[SafetyReviewItem] = []

    def enqueue(self, item: SafetyReviewItem) -> SafetyReviewItem:
        self.items.append(item)
        return item

    def list_pending(self) -> list[SafetyReviewItem]:
        return [item for item in self.items if item.status == "pending"]


class JsonlSafetyReviewQueue:
    """本地 JSONL safety review queue。

    一行一条 review item,适合学习版本地排障和人工复核。它不是协作式审核平台,
    但已经把“需要人工审核”的请求从一次性内存状态变成了可保留证据。
    """

    def __init__(self, path: str | Path = "data/safety-review-queue.jsonl") -> None:
        self.path = Path(path)

    def enqueue(self, item: SafetyReviewItem) -> SafetyReviewItem:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 一行一条 JSON(append-only);sort_keys 让每行键序稳定,方便 diff / grep。
        with self.path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(item.to_dict(), ensure_ascii=False, sort_keys=True) + "\n")
        return item

    def list_pending(self) -> list[SafetyReviewItem]:
        if not self.path.exists():
            return []
        items: list[SafetyReviewItem] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            item = safety_review_item_from_dict(json.loads(line))
            if item.status == "pending":
                items.append(item)
        return items


def build_safety_review_queue_from_env() -> SafetyReviewQueue:
    """按环境变量创建本地 safety review queue。

    可选:
        AGENTIC_SAFETY_REVIEW_QUEUE=memory  默认,只在当前进程内保留。
        AGENTIC_SAFETY_REVIEW_QUEUE=jsonl   追加写入 JSONL。
        AGENTIC_SAFETY_REVIEW_QUEUE_PATH=data/safety-review-queue.jsonl
    """

    mode = os.getenv("AGENTIC_SAFETY_REVIEW_QUEUE", "memory").strip().lower()
    path = os.getenv("AGENTIC_SAFETY_REVIEW_QUEUE_PATH")
    if mode in {"jsonl", "file"} or path:
        return JsonlSafetyReviewQueue(path or "data/safety-review-queue.jsonl")
    return InMemorySafetyReviewQueue()


def make_safety_review_item(
    item_id: str,
    run_id: str,
    goal: str,
    safety_decision: dict[str, Any],
    identity: dict[str, Any],
) -> SafetyReviewItem:
    return SafetyReviewItem(
        id=item_id,
        run_id=run_id,
        goal=goal,
        safety_decision=safety_decision,
        identity=identity,
        created_at=now_iso(),
    )


def safety_review_item_from_dict(data: dict[str, Any]) -> SafetyReviewItem:
    # 兼容两种键风格: to_dict 写出的 camelCase(runId/createdAt),以及可能的 snake_case。
    return SafetyReviewItem(
        id=str(data.get("id", "")),
        run_id=str(data.get("runId") or data.get("run_id") or ""),
        goal=str(data.get("goal", "")),
        safety_decision=_dict_data(data.get("safetyDecision") or data.get("safety_decision")),
        identity=_dict_data(data.get("identity")),
        created_at=str(data.get("createdAt") or data.get("created_at") or ""),
        status=_status(data.get("status")),
        reviewer=_optional_str(data.get("reviewer")),
        reviewed_at=_optional_str(data.get("reviewedAt") or data.get("reviewed_at")),
        notes=_optional_str(data.get("notes")),
    )


def _dict_data(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _status(value: Any) -> SafetyReviewStatus:
    # 只认已知的终态;其它一律回落 pending(未知/损坏值不该被当成"已审核")。
    return value if value in {"approved", "rejected"} else "pending"


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)
