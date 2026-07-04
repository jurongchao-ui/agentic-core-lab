"""memory_lifecycle — 长期记忆生命周期策略的单一真相源。

MemoryPolicy 决定“要不要保存”,MemoryStore 决定“存在哪里”。
本模块负责长期记忆保存后的治理规则:

- 去重/合并用 semantic key。
- 人工治理用 conflict key。
- importance 计算。
- 默认过期时间。
- retention 排序。

把这些规则放在一个模块里,可以避免 MemoryStore、memory_admin、未来 API
各自复制一套相似规则后慢慢漂移。

调用关系图:
  MemoryStore(memory.py) ─▶ MemoryLifecyclePolicy
        semantic key(去重/合并)/ conflict key(人工治理)/ importance / 默认过期 / retention 排序
  memory_admin ─▶ 经 JsonMemoryStore 间接复用同一套规则(不绕过生命周期)。
  定位: 纯规则模块, 被记忆侧调用; 本身不依赖 Agent 运行时。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from .schemas import MemoryRecord


@dataclass(frozen=True)
class MemoryLifecyclePolicy:
    """长期记忆生命周期策略。

    这是学习版的“集中式策略对象”。生产里可以把这些配置从代码迁移到
    租户级 policy JSON、数据库配置或策略中心。
    """

    task_memory_ttl_days: int = 30
    positive_score_keys: tuple[str, ...] = (
        "future_relevance",
        "stability",
        "user_preference",
        "task_continuity",
        "explicit_memory_intent",
        "user_profile",
    )
    type_importance_boosts: dict[str, int] = field(
        default_factory=lambda: {
            "user_profile": 30,
            "preference": 25,
            "task_state": 15,
            "task_context": 15,
            "long_term_note": 10,
        }
    )

    def normalize_text(self, text: str) -> str:
        """规范化正文,用于精确去重和 exact conflict key。"""

        return " ".join(text.strip().split())

    def compact_text(self, text: str) -> str:
        """生成适合规则匹配的紧凑文本。"""

        return (
            self.normalize_text(text)
            .lower()
            .replace(" ", "")
            .replace("，", ",")
            .replace("、", ",")
            .replace("：", ":")
        )

    def semantic_key(self, memory_type: str, text: str) -> str | None:
        """保存/写入时使用的语义合并 key。

        这个 key 要相对保守,因为命中后会直接更新已有 active 记忆。
        """

        compact = self.compact_text(text)
        if memory_type == "user_profile":
            if "技术栈" in compact or "techstack" in compact or "常用技术" in compact:
                return "user_profile:tech_stack"
        if memory_type == "preference":
            if "学习" in compact and ("分钟" in compact or "时长" in compact or "控制" in compact):
                return "preference:study_session_duration"
        return None

    def conflict_key(self, memory: MemoryRecord) -> str | None:
        """人工治理时使用的冲突 key。

        这个 key 可以比 semantic_key 更宽松,因为它只提示“需要审核”,
        不会自动改写记忆内容。
        """

        semantic_key = self.semantic_key(memory.memory_type, memory.text)
        if semantic_key is not None:
            return semantic_key

        compact = self.compact_text(memory.text)
        if memory.memory_type == "preference":
            has_study_context = "学习" in compact or "任务" in compact or "每次" in compact
            has_duration_context = "分钟" in compact or "时长" in compact or "控制" in compact
            if has_study_context and has_duration_context:
                return "preference:study_session_duration"

        normalized = self.normalize_text(memory.text).lower()
        return f"exact:{memory.memory_type}:{normalized}"

    def memory_importance(self, memory_type: str, scores: dict[str, int]) -> int:
        """根据 MemoryPolicy 评分推导 0-100 的长期记忆重要性。"""

        positive_score = sum(_optional_int(scores.get(key), 0) for key in self.positive_score_keys)
        type_boost = self.type_importance_boosts.get(memory_type, 5)
        sensitivity_penalty = _optional_int(scores.get("sensitivity_risk"), 0) * 10
        return clamp_int((positive_score * 8) + type_boost - sensitivity_penalty, 0, 100)

    def default_expiry(self, memory_type: str, created_at: str) -> str | None:
        """给阶段性记忆设置默认过期时间。"""

        if memory_type not in {"task_state", "task_context"}:
            return None
        created = parse_iso(created_at)
        return (created + timedelta(days=self.task_memory_ttl_days)).isoformat()

    def is_expired(self, memory: MemoryRecord, now: datetime) -> bool:
        if memory.expires_at is None:
            return False
        return parse_iso(memory.expires_at) <= now

    def retention_sort_key(self, memory: MemoryRecord) -> tuple[int, int, float]:
        """重要性、访问次数、更新时间共同决定 retention 保留顺序。"""

        timestamp = parse_iso(memory.updated_at or memory.created_at).timestamp()
        return (memory.importance, memory.access_count, timestamp)


def parse_iso(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def clamp_int(value: int, min_value: int, max_value: int) -> int:
    return max(min_value, min(max_value, value))


def _optional_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


DEFAULT_MEMORY_LIFECYCLE_POLICY = MemoryLifecyclePolicy()
