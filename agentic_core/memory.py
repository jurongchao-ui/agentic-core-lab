"""memory — 记忆库(notes/todos/长期记忆/事件)+ 生命周期治理 + 持久化。

功能:
  - MemoryStore(进程内存版): 笔记/待办/长期记忆/事件的读写; snapshot() 给 planner/responder 只读快照。
  - 长期记忆生命周期: 精确 + 规则语义去重(_memory_semantic_key)、active/archived 状态、
    访问统计(touch)、importance、expiresAt 过期归档、retention 数量上限裁剪。
  - 事件: record_event 构造 EventRecord 并交给 EventWriter(内存/JSONL/SQLite);
    写盘前用 redact_event 复用 SENSITIVE_PATTERN 脱敏。
  - JsonMemoryStore(子类): 每次写操作后落盘 data/memory.json, 启动时 load, 实现跨进程记忆持久化。
  - build_memory_store_from_env: 按 AGENTIC_MEMORY_STORE/PATH 选内存版还是 JSON 持久化版。

调用关系图:
  Agent ─▶ MemoryStore.add_long_term_memory / add_note / add_todo / snapshot / record_event
  tools ─▶ MemoryStore.add_note/add_todo/list_todos/add_long_term_memory(经 memory.add 网关)
  record_event ─▶ EventWriter.write(EventRecord)  (event_writer: 内存 + JSONL/SQLite, 可脱敏)
  cli/chat ─▶ build_memory_store_from_env() ─▶ MemoryStore | JsonMemoryStore
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .event_writer import EventWriter, build_event_writer_from_env, redact_event
from .schemas import EventRecord, MemoryRecord, MemorySnapshot, MemoryStatus, NoteRecord, TodoRecord


def now_iso() -> str:
    """返回当前 UTC 时间字符串。

    ISO 格式适合存储和调试,例如 2026-07-01T04:37:43+00:00。
    """
    return datetime.now(timezone.utc).isoformat()


class MemoryStore:
    """进程内存版记忆库。

    v1 先不接数据库/Obsidian,所有数据都放在 Python list 里。
    程序退出后数据会消失。

    这正适合学习:
        先看懂 MemoryStore 的职责,再把底层存储换成 SQLite/Markdown/Obsidian。
    """

    def __init__(self, event_writer: EventWriter | None = None) -> None:
        # 普通学习笔记,由 note.add 工具写入。
        self.notes: list[NoteRecord] = []

        # 待办事项,由 todo.add / todo.list 工具使用。
        self.todos: list[TodoRecord] = []

        # 事件日志,记录每次 memory decision、tool action、final answer。
        self.events: list[EventRecord] = []
        self.event_writer = event_writer or build_event_writer_from_env(self.events)
        self._event_count = 0

        # 长期记忆,由 MemoryPolicy 判断后写入。
        self.long_term_memories: list[MemoryRecord] = []

    def add_note(self, text: str) -> NoteRecord:
        """新增一条学习笔记。"""
        note = NoteRecord(
            id=f"note_{len(self.notes) + 1}",
            text=text,
            created_at=now_iso(),
        )
        self.notes.append(note)
        return note

    def add_todo(self, text: str) -> TodoRecord:
        """新增一条待办。"""
        todo = TodoRecord(
            id=f"todo_{len(self.todos) + 1}",
            text=text,
            done=False,
            created_at=now_iso(),
        )
        self.todos.append(todo)
        return todo

    def list_todos(self) -> list[TodoRecord]:
        """返回待办列表副本。

        用 list(self.todos) 是为了避免外部代码直接修改内部 list。
        """
        return list(self.todos)

    def add_long_term_memory(
        self,
        memory_type: str,
        text: str,
        reason: str,
        scores: dict[str, int],
    ) -> MemoryRecord:
        """新增一条长期记忆。

        精确重复的长期记忆直接返回已有记录,避免用户多次表达同一偏好时不断膨胀。
        规则能识别的同主题记忆会做语义合并,例如“用户技术栈”后续更新时只保留一条 active 记忆。
        """
        scores_data = dict(scores)
        existing = self.find_long_term_memory(memory_type=memory_type, text=text)
        if existing:
            existing.updated_at = now_iso()
            existing.importance = max(existing.importance, _memory_importance(memory_type, scores_data))
            return existing
        semantic_match = self.find_semantic_long_term_memory(memory_type=memory_type, text=text)
        if semantic_match:
            now = now_iso()
            if semantic_match.text not in semantic_match.merged_from:
                semantic_match.merged_from.append(semantic_match.text)
            semantic_match.text = text
            semantic_match.reason = reason
            semantic_match.scores = scores_data
            semantic_match.importance = max(
                semantic_match.importance,
                _memory_importance(memory_type, scores_data),
            )
            semantic_match.expires_at = _default_memory_expiry(memory_type, now)
            semantic_match.updated_at = now
            return semantic_match
        created_at = now_iso()
        memory = MemoryRecord(
            id=f"memory_{len(self.long_term_memories) + 1}",
            memory_type=memory_type,
            text=text,
            reason=reason,
            scores=scores_data,
            created_at=created_at,
            updated_at=created_at,
            importance=_memory_importance(memory_type, scores_data),
            expires_at=_default_memory_expiry(memory_type, created_at),
        )
        self.long_term_memories.append(memory)
        return memory

    def find_long_term_memory(self, memory_type: str, text: str) -> MemoryRecord | None:
        """查找同类型、同正文的长期记忆。"""

        normalized_text = _normalize_memory_text(text)
        for memory in self.long_term_memories:
            if memory.status != "active":
                continue
            if memory.memory_type != memory_type:
                continue
            if _normalize_memory_text(memory.text) == normalized_text:
                return memory
        return None

    def find_semantic_long_term_memory(self, memory_type: str, text: str) -> MemoryRecord | None:
        """查找同类型、同主题的 active 长期记忆。

        这是学习版语义合并:先用可解释规则识别少量高价值主题。
        生产里可以把 `_memory_semantic_key` 替换成 embedding/向量检索或数据库唯一键。
        """

        semantic_key = _memory_semantic_key(memory_type, text)
        if semantic_key is None:
            return None
        for memory in self.long_term_memories:
            if memory.status != "active":
                continue
            if memory.memory_type != memory_type:
                continue
            if _memory_semantic_key(memory.memory_type, memory.text) == semantic_key:
                return memory
        return None

    def list_active_long_term_memories(self) -> list[MemoryRecord]:
        """返回仍会影响 planner 的 active 长期记忆。"""

        self.archive_expired_long_term_memories()
        return [memory for memory in self.long_term_memories if memory.status == "active"]

    def archive_expired_long_term_memories(self, now: str | None = None) -> list[MemoryRecord]:
        """归档已经过期的长期记忆。

        task_state 这类阶段性状态默认会有 expires_at。
        过期后归档而不是删除,方便审计和回放。
        """

        archived: list[MemoryRecord] = []
        now_value = _parse_iso(now or now_iso())
        for memory in self.long_term_memories:
            if memory.status != "active":
                continue
            if not _is_memory_expired(memory, now_value):
                continue
            archived.append(self.archive_long_term_memory(memory.id, "expired"))
        return archived

    def prune_long_term_memories(self, max_active: int, reason: str = "retention_limit") -> list[MemoryRecord]:
        """按重要性保留最多 max_active 条 active 记忆。

        低重要性、低访问次数、更旧的记忆会先被归档。
        这是学习版 retention,不是删除策略。
        """

        if max_active < 0:
            raise ValueError("max_active must be >= 0")
        self.archive_expired_long_term_memories()
        active_memories = self.list_active_long_term_memories()
        if len(active_memories) <= max_active:
            return []
        active_memories.sort(key=_memory_retention_sort_key, reverse=True)
        keep_ids = {memory.id for memory in active_memories[:max_active]}
        archived: list[MemoryRecord] = []
        for memory in active_memories[max_active:]:
            if memory.id not in keep_ids:
                archived.append(self.archive_long_term_memory(memory.id, reason))
        return archived

    def touch_long_term_memory(self, memory_id: str) -> MemoryRecord:
        """记录一条长期记忆被读取过。"""

        memory = self._get_long_term_memory(memory_id)
        memory.last_accessed_at = now_iso()
        memory.access_count += 1
        memory.updated_at = memory.last_accessed_at
        return memory

    def archive_long_term_memory(self, memory_id: str, reason: str) -> MemoryRecord:
        """归档一条长期记忆。

        归档不是删除。记录仍保留在 JSON/内存里,但不再进入 snapshot,
        因此不会继续影响 planner。
        """

        memory = self._get_long_term_memory(memory_id)
        now = now_iso()
        memory.status = "archived"
        memory.archived_at = now
        memory.archive_reason = reason
        memory.updated_at = now
        return memory

    def update_long_term_memory_importance(self, memory_id: str, importance: int) -> MemoryRecord:
        """手动调整长期记忆重要性,方便后续人工审核或 eval 调参。"""

        memory = self._get_long_term_memory(memory_id)
        memory.importance = _clamp_int(importance, 0, 100)
        memory.updated_at = now_iso()
        return memory

    def _get_long_term_memory(self, memory_id: str) -> MemoryRecord:
        for memory in self.long_term_memories:
            if memory.id == memory_id:
                return memory
        raise KeyError(f"unknown long-term memory: {memory_id}")

    def record_event(
        self,
        event: dict[str, Any] | None = None,
        event_type: str | None = None,
        run_id: str | None = None,
        payload: dict[str, Any] | None = None,
        source: str | None = None,
        level: str = "info",
    ) -> EventRecord:
        """记录一条事件。

        兼容旧调用: record_event({"runId": "...", "type": "...", ...})。
        新调用推荐显式传 event_type/run_id/payload。
        """
        data = dict(event or {})
        event_type = event_type or str(data.pop("type", "event"))
        run_id = run_id or str(data.pop("runId", "unknown"))
        source = source or str(data.pop("source", _infer_event_source(event_type)))
        level = str(data.pop("level", level))
        payload = payload if payload is not None else data
        self._event_count += 1
        record = EventRecord(
            id=f"event_{self._event_count}",
            event_type=event_type,
            run_id=run_id,
            payload=payload,
            created_at=now_iso(),
            source=source,
            level=level,
        )
        record = redact_event(record)
        self.event_writer.write(record)
        return record

    def snapshot(self, touch_long_term: bool = False) -> MemorySnapshot:
        """返回当前记忆快照,用于打印和传给 planner。"""
        active_memories = self.list_active_long_term_memories()
        if touch_long_term:
            active_memories = [self.touch_long_term_memory(memory.id) for memory in active_memories]
        return MemorySnapshot(
            notes=list(self.notes),
            todos=list(self.todos),
            long_term_memories=active_memories,
            recent_events=self.events[-10:],
        )


class JsonMemoryStore(MemoryStore):
    """JSON 文件版记忆库。

    它仍然复用 MemoryStore 的 list 数据结构,只是在每次写入后把快照保存到磁盘。
    这样核心业务代码不用知道底层到底是内存还是文件。
    """

    def __init__(self, path: str | Path = "data/memory.json", event_writer: EventWriter | None = None) -> None:
        self.path = Path(path)
        super().__init__(event_writer=event_writer)
        self.load()

    def load(self) -> None:
        """从 JSON 文件加载记忆。

        文件不存在时保持空内存,不报错。
        """

        if not self.path.exists():
            return
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("memory json root must be an object")

        self.notes[:] = [_note_from_dict(item) for item in _list_data(data, "notes")]
        self.todos[:] = [_todo_from_dict(item) for item in _list_data(data, "todos")]
        self.long_term_memories[:] = [
            _memory_from_dict(item) for item in _list_data(data, "long_term_memories", "longTermMemories")
        ]
        self.events[:] = [_event_from_dict(item) for item in _list_data(data, "events")]
        self._event_count = _max_record_number(self.events, "event_")

    def save(self) -> None:
        """把当前记忆快照写入 JSON 文件。

        采用临时文件 + replace,避免写到一半中断时留下半个 JSON。
        """

        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "schemaVersion": 1,
            "notes": [note.to_dict() for note in self.notes],
            "todos": [todo.to_dict() for todo in self.todos],
            "long_term_memories": [memory.to_dict() for memory in self.long_term_memories],
            "events": [event.to_dict() for event in self.events],
        }
        temp_path = self.path.with_name(f"{self.path.name}.tmp")
        temp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temp_path.replace(self.path)

    def add_note(self, text: str) -> NoteRecord:
        note = super().add_note(text)
        self.save()
        return note

    def add_todo(self, text: str) -> TodoRecord:
        todo = super().add_todo(text)
        self.save()
        return todo

    def add_long_term_memory(
        self,
        memory_type: str,
        text: str,
        reason: str,
        scores: dict[str, int],
    ) -> MemoryRecord:
        memory = super().add_long_term_memory(memory_type, text, reason, scores)
        self.save()
        return memory

    def touch_long_term_memory(self, memory_id: str) -> MemoryRecord:
        memory = super().touch_long_term_memory(memory_id)
        self.save()
        return memory

    def archive_long_term_memory(self, memory_id: str, reason: str) -> MemoryRecord:
        memory = super().archive_long_term_memory(memory_id, reason)
        self.save()
        return memory

    def archive_expired_long_term_memories(self, now: str | None = None) -> list[MemoryRecord]:
        archived = super().archive_expired_long_term_memories(now)
        if archived:
            self.save()
        return archived

    def prune_long_term_memories(self, max_active: int, reason: str = "retention_limit") -> list[MemoryRecord]:
        archived = super().prune_long_term_memories(max_active, reason)
        if archived:
            self.save()
        return archived

    def update_long_term_memory_importance(self, memory_id: str, importance: int) -> MemoryRecord:
        memory = super().update_long_term_memory_importance(memory_id, importance)
        self.save()
        return memory

    def record_event(
        self,
        event: dict[str, Any] | None = None,
        event_type: str | None = None,
        run_id: str | None = None,
        payload: dict[str, Any] | None = None,
        source: str | None = None,
        level: str = "info",
    ) -> EventRecord:
        record = super().record_event(
            event=event,
            event_type=event_type,
            run_id=run_id,
            payload=payload,
            source=source,
            level=level,
        )
        self.save()
        return record


def build_memory_store_from_env() -> MemoryStore:
    """根据环境变量创建 MemoryStore。

    默认是进程内存,保持原学习体验不变。

    开启 JSON 持久化:
        AGENTIC_MEMORY_STORE=json
        AGENTIC_MEMORY_PATH=data/memory.json
    """

    memory_store = os.getenv("AGENTIC_MEMORY_STORE", "memory").lower()
    memory_path = os.getenv("AGENTIC_MEMORY_PATH")
    if memory_store in {"json", "file", "persistent"} or memory_path:
        return JsonMemoryStore(memory_path or "data/memory.json")
    return MemoryStore()


def _infer_event_source(event_type: str) -> str:
    """从事件类型推断来源模块,减少 call site 的重复参数。"""

    if event_type.startswith("memory_"):
        return "memory"
    if event_type.startswith("safety_"):
        return "safety"
    if event_type.startswith("tool_"):
        return "tool"
    if event_type.startswith("response_"):
        return "response"
    if event_type.startswith("planner_"):
        return "planner"
    return "agent"


def _normalize_memory_text(text: str) -> str:
    """用于精确去重的轻量规范化。"""

    return " ".join(text.strip().split())


def _list_data(data: dict[str, Any], *keys: str) -> list[Any]:
    """从 JSON dict 里取 list,同时兼容新旧字段名。"""

    for key in keys:
        value = data.get(key)
        if isinstance(value, list):
            return value
    return []


def _note_from_dict(data: Any) -> NoteRecord:
    item = data if isinstance(data, dict) else {}
    return NoteRecord(
        id=str(item.get("id", "")),
        text=str(item.get("text", "")),
        created_at=str(item.get("createdAt") or item.get("created_at") or ""),
    )


def _todo_from_dict(data: Any) -> TodoRecord:
    item = data if isinstance(data, dict) else {}
    return TodoRecord(
        id=str(item.get("id", "")),
        text=str(item.get("text", "")),
        done=bool(item.get("done", False)),
        created_at=str(item.get("createdAt") or item.get("created_at") or ""),
    )


def _memory_from_dict(data: Any) -> MemoryRecord:
    item = data if isinstance(data, dict) else {}
    scores = item.get("scores", {})
    access_count = item.get("accessCount") or item.get("access_count") or 0
    return MemoryRecord(
        id=str(item.get("id", "")),
        memory_type=str(item.get("type") or item.get("memory_type") or "note"),
        text=str(item.get("text", "")),
        reason=str(item.get("reason", "")),
        scores=scores if isinstance(scores, dict) else {},
        created_at=str(item.get("createdAt") or item.get("created_at") or ""),
        updated_at=_optional_str(item.get("updatedAt") or item.get("updated_at")),
        last_accessed_at=_optional_str(item.get("lastAccessedAt") or item.get("last_accessed_at")),
        access_count=access_count if isinstance(access_count, int) else 0,
        status=_memory_status(item.get("status")),
        archived_at=_optional_str(item.get("archivedAt") or item.get("archived_at")),
        archive_reason=_optional_str(item.get("archiveReason") or item.get("archive_reason")),
        importance=_optional_int(item.get("importance"), default=0),
        expires_at=_optional_str(item.get("expiresAt") or item.get("expires_at")),
        merged_from=_string_list(item.get("mergedFrom") or item.get("merged_from")),
    )


def _event_from_dict(data: Any) -> EventRecord:
    item = data if isinstance(data, dict) else {}
    payload = item.get("payload", {})
    return EventRecord(
        id=str(item.get("id", "")),
        event_type=str(item.get("type") or item.get("event_type") or "event"),
        run_id=str(item.get("runId") or item.get("run_id") or "unknown"),
        payload=payload if isinstance(payload, dict) else {"value": payload},
        created_at=str(item.get("createdAt") or item.get("created_at") or ""),
        source=str(item.get("source", "agent")),
        level=str(item.get("level", "info")),
        schema_version=int(item.get("schemaVersion") or item.get("schema_version") or 1),
        redacted=bool(item.get("redacted", False)),
    )


def _max_record_number(records: list[EventRecord], prefix: str) -> int:
    """从 event_12 这类 id 中恢复当前计数。"""

    max_value = 0
    for record in records:
        if not record.id.startswith(prefix):
            continue
        suffix = record.id.removeprefix(prefix)
        if suffix.isdigit():
            max_value = max(max_value, int(suffix))
    return max_value


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _optional_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _memory_status(value: Any) -> MemoryStatus:
    return "archived" if value == "archived" else "active"


def _memory_importance(memory_type: str, scores: dict[str, int]) -> int:
    """根据 MemoryPolicy 评分推导 0-100 的重要性。

    重要性不是“是否保存”的二次判断;是否保存已经由 MemoryPolicy 决定。
    它用于后续排序、retention 和人工审核。
    """

    positive_keys = {
        "future_relevance",
        "stability",
        "user_preference",
        "task_continuity",
        "explicit_memory_intent",
        "user_profile",
    }
    positive_score = sum(_optional_int(scores.get(key), 0) for key in positive_keys)
    type_boost = {
        "user_profile": 30,
        "preference": 25,
        "task_state": 15,
        "task_context": 15,
        "long_term_note": 10,
    }.get(memory_type, 5)
    sensitivity_penalty = _optional_int(scores.get("sensitivity_risk"), 0) * 10
    return _clamp_int((positive_score * 8) + type_boost - sensitivity_penalty, 0, 100)


def _default_memory_expiry(memory_type: str, created_at: str) -> str | None:
    """给阶段性记忆设置默认过期时间。

    用户资料和长期偏好通常不自动过期;任务状态默认 30 天后归档。
    """

    if memory_type not in {"task_state", "task_context"}:
        return None
    created = _parse_iso(created_at)
    return (created + timedelta(days=30)).isoformat()


def _is_memory_expired(memory: MemoryRecord, now: datetime) -> bool:
    if memory.expires_at is None:
        return False
    return _parse_iso(memory.expires_at) <= now


def _parse_iso(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _memory_retention_sort_key(memory: MemoryRecord) -> tuple[int, int, float]:
    timestamp = _parse_iso(memory.updated_at or memory.created_at).timestamp()
    return (memory.importance, memory.access_count, timestamp)


def _memory_semantic_key(memory_type: str, text: str) -> str | None:
    normalized = _normalize_memory_text(text).lower()
    compact = (
        normalized.replace(" ", "")
        .replace("，", ",")
        .replace("、", ",")
        .replace("：", ":")
    )
    if memory_type == "user_profile":
        if "技术栈" in compact or "techstack" in compact or "常用技术" in compact:
            return "user_profile:tech_stack"
    if memory_type == "preference":
        if "学习" in compact and ("分钟" in compact or "时长" in compact or "控制" in compact):
            return "preference:study_session_duration"
    return None


def _clamp_int(value: int, min_value: int, max_value: int) -> int:
    return max(min_value, min(max_value, value))
