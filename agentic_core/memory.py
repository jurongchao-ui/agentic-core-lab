from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


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

    def __init__(self) -> None:
        # 普通学习笔记,由 note.add 工具写入。
        self.notes: list[dict[str, Any]] = []

        # 待办事项,由 todo.add / todo.list 工具使用。
        self.todos: list[dict[str, Any]] = []

        # 事件日志,记录每次 memory decision、tool action、final answer。
        self.events: list[dict[str, Any]] = []

        # 长期记忆,由 MemoryPolicy 判断后写入。
        self.long_term_memories: list[dict[str, Any]] = []

    def add_note(self, text: str) -> dict[str, Any]:
        """新增一条学习笔记。"""
        note = {
            "id": f"note_{len(self.notes) + 1}",
            "text": text,
            "createdAt": now_iso(),
        }
        self.notes.append(note)
        return note

    def add_todo(self, text: str) -> dict[str, Any]:
        """新增一条待办。"""
        todo = {
            "id": f"todo_{len(self.todos) + 1}",
            "text": text,
            "done": False,
            "createdAt": now_iso(),
        }
        self.todos.append(todo)
        return todo

    def list_todos(self) -> list[dict[str, Any]]:
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
    ) -> dict[str, Any]:
        """新增一条长期记忆。"""
        memory = {
            "id": f"memory_{len(self.long_term_memories) + 1}",
            "type": memory_type,
            "text": text,
            "reason": reason,
            "scores": dict(scores),
            "createdAt": now_iso(),
        }
        self.long_term_memories.append(memory)
        return memory

    def record_event(self, event: dict[str, Any]) -> None:
        """记录一条事件,自动补 createdAt。"""
        self.events.append({**event, "createdAt": now_iso()})

    def snapshot(self) -> dict[str, Any]:
        """返回当前记忆快照,用于打印和传给 planner。"""
        return {
            "notes": list(self.notes),
            "todos": list(self.todos),
            "longTermMemories": list(self.long_term_memories),
            "recentEvents": self.events[-10:],
        }
