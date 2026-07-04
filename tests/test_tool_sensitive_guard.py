from __future__ import annotations

import pytest

from agentic_core.memory.store import MemoryStore
from agentic_core.policies.memory import RuleBasedMemoryPolicy
from agentic_core.tools.registry import ToolRegistry


def build_registry() -> tuple[ToolRegistry, MemoryStore]:
    memory = MemoryStore()
    return ToolRegistry(memory, RuleBasedMemoryPolicy()), memory


def test_note_add_rejects_sensitive_and_does_not_persist() -> None:
    registry, memory = build_registry()
    with pytest.raises(ValueError):
        registry.execute("note.add", {"text": "请记住我的密码是 abcd1234"})
    assert memory.notes == []


def test_todo_add_rejects_sensitive_and_does_not_persist() -> None:
    registry, memory = build_registry()
    with pytest.raises(ValueError):
        registry.execute("todo.add", {"text": "把银行卡号 6222xxxx 记下来"})
    assert memory.todos == []


def test_note_add_allows_normal_text() -> None:
    registry, memory = build_registry()
    registry.execute("note.add", {"text": "计算 128 * 7 = 896"})
    assert len(memory.notes) == 1


def test_error_message_does_not_echo_sensitive_input() -> None:
    registry, _ = build_registry()
    with pytest.raises(ValueError) as excinfo:
        registry.execute("note.add", {"text": "我的密码是 abcd1234"})
    assert "abcd1234" not in str(excinfo.value)
