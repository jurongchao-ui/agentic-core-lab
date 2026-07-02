from __future__ import annotations

import json
import os
from datetime import datetime, timezone
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
        """
        existing = self.find_long_term_memory(memory_type=memory_type, text=text)
        if existing:
            existing.updated_at = now_iso()
            return existing
        created_at = now_iso()
        memory = MemoryRecord(
            id=f"memory_{len(self.long_term_memories) + 1}",
            memory_type=memory_type,
            text=text,
            reason=reason,
            scores=dict(scores),
            created_at=created_at,
            updated_at=created_at,
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

    def list_active_long_term_memories(self) -> list[MemoryRecord]:
        """返回仍会影响 planner 的 active 长期记忆。"""

        return [memory for memory in self.long_term_memories if memory.status == "active"]

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


def _memory_status(value: Any) -> MemoryStatus:
    return "archived" if value == "archived" else "active"
