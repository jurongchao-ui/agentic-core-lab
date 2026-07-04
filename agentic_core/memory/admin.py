"""memory_admin — 长期记忆的本地审核/维护 CLI。

MemoryPolicy 负责“是否保存”,MemoryStore 负责“如何保存”。生产里还需要第三层:
人工或治理流程可以查看、归档、调权重。这里先用标准库实现本地版:

  - list: 按 user/tenant namespace 查看长期记忆。
  - archive: 软归档一条记忆,不物理删除。
  - set-importance: 手动调整 importance,影响后续 retention。
  - conflicts: 查看同 namespace 下 active 长期记忆的冲突组。
  - resolve-conflict: 保留一条记忆,软归档同冲突组里的其他记忆。

它复用 JsonMemoryStore,所以不会绕过生命周期规则。

调用关系图:
  CLI: python -m agentic_core.memory_admin (list | archive | set-importance | conflicts | resolve-conflict)
      └─▶ JsonMemoryStore(memory.py) —— 读/软归档/调 importance, 走 memory_lifecycle 同一套规则
      └─▶ find_memory_conflicts / resolve_memory_conflict —— 同 namespace 冲突组的查看与消解
  定位: 与 Agent.run 解耦的离线维护工具, 只作用于已落盘的 data/memory.json。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from agentic_core.memory.store import JsonMemoryStore
from agentic_core.runtime.schemas import MemoryRecord


def find_memory_conflicts(
    store: JsonMemoryStore,
    user_id: str,
    tenant_id: str,
) -> list[dict[str, Any]]:
    """查找同 namespace 下的 active 长期记忆冲突。

    这里的“冲突”不是说两条记忆一定矛盾,而是它们会竞争同一个长期事实槽位。
    例如用户技术栈只能有一个当前版本,学习时长偏好也应该保留一个当前版本。
    """

    groups: dict[str, list[MemoryRecord]] = {}
    for memory in list_memories(store, user_id, tenant_id):
        key = store.lifecycle_policy.conflict_key(memory)
        if key is None:
            continue
        groups.setdefault(key, []).append(memory)

    conflicts: list[dict[str, Any]] = []
    for key in sorted(groups):
        memories = groups[key]
        if len(memories) <= 1:
            continue
        conflicts.append(
            {
                "conflictId": f"conflict_{len(conflicts) + 1}",
                "key": key,
                "userId": user_id,
                "tenantId": tenant_id,
                "count": len(memories),
                "memories": [memory.to_dict() for memory in memories],
            }
        )
    return conflicts


def resolve_memory_conflict(
    store: JsonMemoryStore,
    user_id: str,
    tenant_id: str,
    keep_memory_id: str,
    reason: str,
) -> dict[str, Any]:
    """解决一组冲突:保留 keep_memory_id,归档同组的其他 active 记忆。"""

    keep_memory = _require_memory_in_namespace(store, keep_memory_id, user_id, tenant_id)
    if keep_memory.status != "active":
        raise ValueError("keep memory must be active")

    conflict_group = _find_conflict_group_containing(store, user_id, tenant_id, keep_memory_id)
    if conflict_group is None:
        raise ValueError(f"memory is not part of an active conflict: {keep_memory_id}")

    archived: list[MemoryRecord] = []
    for memory in conflict_group:
        if memory.id == keep_memory_id:
            continue
        archived.append(store.archive_long_term_memory(memory.id, reason))

    return {
        "type": "agentic_memory_review_resolve_conflict",
        "userId": user_id,
        "tenantId": tenant_id,
        "keptMemory": keep_memory.to_dict(),
        "archivedMemories": [memory.to_dict() for memory in archived],
        "archivedCount": len(archived),
    }


def list_memories(
    store: JsonMemoryStore,
    user_id: str,
    tenant_id: str,
    include_archived: bool = False,
    memory_type: str | None = None,
) -> list[MemoryRecord]:
    """按 namespace 列出长期记忆。"""

    memories: list[MemoryRecord] = []
    for memory in store.long_term_memories:
        if memory.user_id != user_id or memory.tenant_id != tenant_id:
            continue
        if not include_archived and memory.status != "active":
            continue
        if memory_type is not None and memory.memory_type != memory_type:
            continue
        memories.append(memory)
    return memories


def archive_memory(
    store: JsonMemoryStore,
    memory_id: str,
    reason: str,
    user_id: str,
    tenant_id: str,
) -> MemoryRecord:
    """在指定 namespace 内归档一条长期记忆。"""

    _require_memory_in_namespace(store, memory_id, user_id, tenant_id)
    return store.archive_long_term_memory(memory_id, reason)


def set_memory_importance(
    store: JsonMemoryStore,
    memory_id: str,
    importance: int,
    user_id: str,
    tenant_id: str,
) -> MemoryRecord:
    """在指定 namespace 内调整一条长期记忆的重要性。"""

    _require_memory_in_namespace(store, memory_id, user_id, tenant_id)
    return store.update_long_term_memory_importance(memory_id, importance)


def _find_conflict_group_containing(
    store: JsonMemoryStore,
    user_id: str,
    tenant_id: str,
    memory_id: str,
) -> list[MemoryRecord] | None:
    for conflict in find_memory_conflicts(store, user_id, tenant_id):
        memories = [
            _require_memory_in_namespace(store, item["id"], user_id, tenant_id)
            for item in conflict["memories"]
        ]
        if any(memory.id == memory_id for memory in memories):
            return memories
    return None


def _require_memory_in_namespace(
    store: JsonMemoryStore,
    memory_id: str,
    user_id: str,
    tenant_id: str,
) -> MemoryRecord:
    for memory in store.long_term_memories:
        if memory.id != memory_id:
            continue
        if memory.user_id == user_id and memory.tenant_id == tenant_id:
            return memory
        raise ValueError("memory does not belong to the requested namespace")
    raise ValueError(f"unknown memory: {memory_id}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Review and maintain long-term memories")
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="列出长期记忆")
    _add_common_args(list_parser)
    list_parser.add_argument("--include-archived", action="store_true", help="包含已归档记忆")
    list_parser.add_argument("--type", dest="memory_type", help="按记忆类型过滤")
    list_parser.add_argument("--json", action="store_true", help="输出 JSON")

    archive_parser = subparsers.add_parser("archive", help="归档长期记忆")
    _add_common_args(archive_parser)
    archive_parser.add_argument("--memory-id", required=True, help="长期记忆 id")
    archive_parser.add_argument("--reason", required=True, help="归档原因")
    archive_parser.add_argument("--json", action="store_true", help="输出 JSON")

    importance_parser = subparsers.add_parser("set-importance", help="设置长期记忆 importance")
    _add_common_args(importance_parser)
    importance_parser.add_argument("--memory-id", required=True, help="长期记忆 id")
    importance_parser.add_argument("--importance", type=int, required=True, help="importance 0-100")
    importance_parser.add_argument("--json", action="store_true", help="输出 JSON")

    conflicts_parser = subparsers.add_parser("conflicts", help="查看长期记忆冲突")
    _add_common_args(conflicts_parser)
    conflicts_parser.add_argument("--json", action="store_true", help="输出 JSON")

    resolve_parser = subparsers.add_parser("resolve-conflict", help="解决长期记忆冲突")
    _add_common_args(resolve_parser)
    resolve_parser.add_argument("--keep-memory-id", required=True, help="要保留的长期记忆 id")
    resolve_parser.add_argument("--reason", required=True, help="归档其他冲突记忆的原因")
    resolve_parser.add_argument("--json", action="store_true", help="输出 JSON")

    args = parser.parse_args(argv)
    store = JsonMemoryStore(args.path)

    if args.command == "list":
        memories = list_memories(
            store,
            user_id=args.user_id,
            tenant_id=args.tenant_id,
            include_archived=args.include_archived,
            memory_type=args.memory_type,
        )
        result = {
            "type": "agentic_memory_review_list",
            "path": str(Path(args.path)),
            "userId": args.user_id,
            "tenantId": args.tenant_id,
            "includeArchived": args.include_archived,
            "count": len(memories),
            "memories": [memory.to_dict() for memory in memories],
        }
        _print_result(result, args.json, _format_memory_list(memories))
        return 0

    if args.command == "archive":
        memory = archive_memory(
            store,
            memory_id=args.memory_id,
            reason=args.reason,
            user_id=args.user_id,
            tenant_id=args.tenant_id,
        )
        result = {
            "type": "agentic_memory_review_archive",
            "memory": memory.to_dict(),
        }
        _print_result(result, args.json, f"Archived {memory.id}: {memory.archive_reason}")
        return 0

    if args.command == "set-importance":
        memory = set_memory_importance(
            store,
            memory_id=args.memory_id,
            importance=args.importance,
            user_id=args.user_id,
            tenant_id=args.tenant_id,
        )
        result = {
            "type": "agentic_memory_review_importance",
            "memory": memory.to_dict(),
        }
        _print_result(result, args.json, f"Updated {memory.id}: importance={memory.importance}")
        return 0

    if args.command == "conflicts":
        conflicts = find_memory_conflicts(
            store,
            user_id=args.user_id,
            tenant_id=args.tenant_id,
        )
        result = {
            "type": "agentic_memory_review_conflicts",
            "path": str(Path(args.path)),
            "userId": args.user_id,
            "tenantId": args.tenant_id,
            "count": len(conflicts),
            "conflicts": conflicts,
        }
        _print_result(result, args.json, _format_conflicts(conflicts))
        return 0

    result = resolve_memory_conflict(
        store,
        user_id=args.user_id,
        tenant_id=args.tenant_id,
        keep_memory_id=args.keep_memory_id,
        reason=args.reason,
    )
    _print_result(
        result,
        args.json,
        f"Kept {result['keptMemory']['id']}; archived {result['archivedCount']} conflicting memories.",
    )
    return 0


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--path", default="data/memory.json", help="memory JSON 路径")
    parser.add_argument("--user-id", default="local_user", help="用户 id")
    parser.add_argument("--tenant-id", default="default_tenant", help="租户 id")


def _print_result(data: dict[str, Any], as_json: bool, text: str) -> None:
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(text)


def _format_memory_list(memories: list[MemoryRecord]) -> str:
    if not memories:
        return "No long-term memories."
    lines = ["Long-Term Memories"]
    for memory in memories:
        lines.append(
            f"- {memory.id}: {memory.memory_type} status={memory.status} "
            f"importance={memory.importance} text={memory.text}"
        )
    return "\n".join(lines)


def _format_conflicts(conflicts: list[dict[str, Any]]) -> str:
    if not conflicts:
        return "No active long-term memory conflicts."
    lines = ["Long-Term Memory Conflicts"]
    for conflict in conflicts:
        lines.append(f"- {conflict['conflictId']}: {conflict['key']} count={conflict['count']}")
        for memory in conflict["memories"]:
            lines.append(f"  - {memory['id']}: {memory['text']}")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
