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

import argparse
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from agentic_core.runtime.schemas import MemoryRecord


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

    @classmethod
    def from_file(cls, path: str | Path) -> "MemoryLifecyclePolicy":
        """从 JSON 文件加载生命周期策略。

        文件格式是增量配置:只写需要覆盖的字段,未写字段继续使用默认值。
        """

        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("memory lifecycle policy file must be a JSON object")
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MemoryLifecyclePolicy":
        """从 dict 构造策略,用于测试、JSON 配置和未来策略中心。"""

        schema_version = data.get("schemaVersion", 1)
        if schema_version != 1:
            raise ValueError("memory lifecycle policy schemaVersion must be 1")

        defaults = cls()
        task_memory_ttl_days = _optional_int(data.get("taskMemoryTtlDays"), defaults.task_memory_ttl_days)
        if task_memory_ttl_days < 0:
            raise ValueError("taskMemoryTtlDays must be >= 0")

        raw_positive_score_keys = data.get("positiveScoreKeys")
        if raw_positive_score_keys is None:
            positive_score_keys = defaults.positive_score_keys
        elif isinstance(raw_positive_score_keys, list) and all(isinstance(item, str) for item in raw_positive_score_keys):
            positive_score_keys = tuple(raw_positive_score_keys)
        else:
            raise ValueError("positiveScoreKeys must be a list of strings")

        type_importance_boosts = dict(defaults.type_importance_boosts)
        raw_boosts = data.get("typeImportanceBoosts")
        if raw_boosts is not None:
            if not isinstance(raw_boosts, dict):
                raise ValueError("typeImportanceBoosts must be an object")
            for memory_type, raw_boost in raw_boosts.items():
                if not isinstance(memory_type, str) or not memory_type:
                    raise ValueError("typeImportanceBoosts keys must be non-empty strings")
                boost = _optional_int(raw_boost, default=-1)
                if boost < 0:
                    raise ValueError(f"typeImportanceBoosts.{memory_type} must be >= 0")
                type_importance_boosts[memory_type] = boost

        return cls(
            task_memory_ttl_days=task_memory_ttl_days,
            positive_score_keys=positive_score_keys,
            type_importance_boosts=type_importance_boosts,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": 1,
            "taskMemoryTtlDays": self.task_memory_ttl_days,
            "positiveScoreKeys": list(self.positive_score_keys),
            "typeImportanceBoosts": dict(self.type_importance_boosts),
        }

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


def load_memory_lifecycle_policy(path: str | Path | None = None) -> MemoryLifecyclePolicy:
    """加载策略。path 为空时返回默认策略。"""

    if path is None:
        return DEFAULT_MEMORY_LIFECYCLE_POLICY
    return MemoryLifecyclePolicy.from_file(path)


def validate_memory_lifecycle_policy_file(path: str | Path) -> dict[str, Any]:
    """校验策略文件,返回适合 CLI/CI 使用的结构化报告。"""

    try:
        policy = MemoryLifecyclePolicy.from_file(path)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        return {
            "schemaVersion": 1,
            "type": "agentic_memory_lifecycle_policy_validation",
            "path": str(Path(path)),
            "valid": False,
            "errors": [str(exc)],
        }
    return {
        "schemaVersion": 1,
        "type": "agentic_memory_lifecycle_policy_validation",
        "path": str(Path(path)),
        "valid": True,
        "errors": [],
        "policy": policy.to_dict(),
    }


def format_memory_lifecycle_policy(policy: MemoryLifecyclePolicy, path: str | Path | None = None) -> str:
    """格式化策略,供人读 CLI 输出使用。"""

    title = "Memory Lifecycle Policy"
    if path is not None:
        title = f"{title}: {path}"
    lines = [
        title,
        f"- taskMemoryTtlDays: {policy.task_memory_ttl_days}",
        f"- positiveScoreKeys: {', '.join(policy.positive_score_keys)}",
        "- typeImportanceBoosts:",
    ]
    for memory_type, boost in sorted(policy.type_importance_boosts.items()):
        lines.append(f"  - {memory_type}: {boost}")
    return "\n".join(lines)


def format_policy_validation(report: dict[str, Any]) -> str:
    """格式化策略校验报告。"""

    lines = [
        "Memory Lifecycle Policy Validation",
        f"- path: {report.get('path', '')}",
        f"- valid: {report.get('valid', False)}",
    ]
    errors = report.get("errors")
    if isinstance(errors, list) and errors:
        lines.append("- errors:")
        for error in errors:
            lines.append(f"  - {error}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect and validate memory lifecycle policy")
    subparsers = parser.add_subparsers(dest="command", required=True)

    show_parser = subparsers.add_parser("show", help="显示默认或指定的长期记忆生命周期策略")
    show_parser.add_argument("--path", help="memory lifecycle policy JSON 路径")
    show_parser.add_argument("--json", action="store_true", help="输出 JSON")

    validate_parser = subparsers.add_parser("validate", help="校验长期记忆生命周期策略 JSON")
    validate_parser.add_argument("--path", required=True, help="memory lifecycle policy JSON 路径")
    validate_parser.add_argument("--json", action="store_true", help="输出 JSON")

    args = parser.parse_args(argv)
    if args.command == "show":
        policy = load_memory_lifecycle_policy(args.path)
        data = {
            "schemaVersion": 1,
            "type": "agentic_memory_lifecycle_policy",
            "path": args.path,
            "policy": policy.to_dict(),
        }
        if args.json:
            print(json.dumps(data, ensure_ascii=False, indent=2))
        else:
            print(format_memory_lifecycle_policy(policy, args.path))
        return 0

    report = validate_memory_lifecycle_policy_file(args.path)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(format_policy_validation(report))
    return 0 if report["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
