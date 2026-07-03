"""event_writer — 事件写入后端(内存 / JSONL / SQLite / 组合)+ 脱敏。

功能:
  - EventWriter 协议 + 三个实现:
      MemoryEventWriter    —— 写进程内 events 列表(默认,保持原行为)。
      JsonlEventWriter     —— 追加写 data/events.jsonl,含按大小轮转 + 文件锁(fcntl,
                              非 POSIX 平台降级无锁)。
      SQLiteEventWriter    —— 写入本地 SQLite,方便按 runId/type/createdAt 查询。
      CompositeEventWriter —— 多路写;单个 writer 抛错只记 warning,不影响用户主流程。
  - build_event_writer_from_env 按 AGENTIC_EVENT_LOG* 环境变量装配 writer(默认仅内存)。
  - redact_event / redact_value 复用 memory_policy.SENSITIVE_PATTERN 对 payload 递归脱敏,
    避免项目里出现两套敏感规则。

调用关系图:
  MemoryStore(装配时)─▶ build_event_writer_from_env(events) ─▶ Memory / Jsonl / Composite writer
  MemoryStore.record_event(...) ─▶ writer.write(EventRecord) ─▶ [内存列表 + JSONL / SQLite]
  下游读取: event_log 读 data/events.jsonl 或 data/events.db 重建时间线; eval_harness 统计事件指标。
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import types
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any
from typing import Protocol

from .memory_policy import SENSITIVE_PATTERN
from .schemas import EventRecord

fcntl: types.ModuleType | None
try:
    import fcntl as _fcntl_module
except ImportError:  # pragma: no cover - Windows 等非 POSIX 平台会走无锁降级
    fcntl = None
else:
    fcntl = _fcntl_module


class EventWriter(Protocol):
    """事件写入接口。

    生产级后端可以是 JSONL、SQLite、Postgres、OTel。
    阶段 1 只提供内存实现,保持现有行为不变。
    """

    def write(self, event: EventRecord) -> None:
        """写入一条事件。"""
        ...


class MemoryEventWriter:
    """把事件写入当前进程内存列表。"""

    def __init__(self, events: list[EventRecord]) -> None:
        self.events = events

    def write(self, event: EventRecord) -> None:
        self.events.append(event)


class JsonlEventWriter:
    """把事件追加写入 JSONL 文件。

    JSONL 的规则是“一行一条 JSON”:
        - 某一行坏了,不会破坏整个文件。
        - 适合 grep、tail、按 runId 过滤。
        - 后续迁移到 SQLite/Postgres/ClickHouse 时也容易批量导入。
    """

    def __init__(
        self,
        path: str | Path,
        max_bytes: int | None = None,
        backup_count: int = 3,
        use_lock: bool = True,
    ) -> None:
        self.path = Path(path)
        self.max_bytes = max_bytes if max_bytes and max_bytes > 0 else None
        self.backup_count = max(0, backup_count)
        self.use_lock = use_lock
        self.lock_path = self.path.with_name(f"{self.path.name}.lock")

    def write(self, event: EventRecord) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True) + "\n"
        with self._locked():
            self._rotate_if_needed(len(line.encode("utf-8")))
            with self.path.open("a", encoding="utf-8") as file:
                file.write(line)

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        """保护 JSONL 轮转和追加写入。

        生产里可能同时有 CLI、chat、后台任务写同一个 events.jsonl。
        轮转和 append 必须处在同一个临界区里,否则两个进程可能同时判断需要轮转,
        导致备份文件互相覆盖。

        macOS/Linux 使用 fcntl.flock。没有 fcntl 的平台降级为无锁写入,但仍保持功能可用。
        """

        if not self.use_lock:
            yield
            return

        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a", encoding="utf-8") as lock_file:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _rotate_if_needed(self, incoming_bytes: int) -> None:
        """按大小轮转 JSONL 文件。

        例如 events.jsonl -> events.jsonl.1 -> events.jsonl.2。
        这是学习版 retention,不是完整生产文件锁方案。
        """

        if self.max_bytes is None or not self.path.exists():
            return
        if self.path.stat().st_size + incoming_bytes <= self.max_bytes:
            return
        if self.backup_count == 0:
            self.path.write_text("", encoding="utf-8")
            return

        oldest = self._backup_path(self.backup_count)
        if oldest.exists():
            oldest.unlink()
        for index in range(self.backup_count - 1, 0, -1):
            source = self._backup_path(index)
            if source.exists():
                source.replace(self._backup_path(index + 1))
        self.path.replace(self._backup_path(1))

    def _backup_path(self, index: int) -> Path:
        return self.path.with_name(f"{self.path.name}.{index}")


class SQLiteEventWriter:
    """把事件写入本地 SQLite 数据库。

    SQLite 比 JSONL 更适合“按 runId / type / createdAt 查询”,但仍然是本地学习版后端。
    这里不改变 EventRecord 的结构,只是把同一份 to_dict() 结果同时保存为:
        - event_json: 完整事件 JSON,保证未来 reader 可以原样还原。
        - payload_json: payload 单独列,方便后续调试或迁移。

    注意主键使用 (run_id, id),而不是单独 id。
    因为当前 MemoryStore 的事件 id 是进程内递增的 event_1/event_2;
    多个 CLI 进程写同一个 SQLite 文件时,不同 run 里都可能出现 event_1。
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._ensure_schema()

    def write(self, event: EventRecord) -> None:
        event_dict = event.to_dict()
        payload = event_dict.get("payload")
        if not isinstance(payload, dict):
            payload = {"value": payload}

        connection = self._connect()
        try:
            with connection:
                connection.execute(
                    """
                    INSERT OR REPLACE INTO events (
                        run_id,
                        id,
                        type,
                        created_at,
                        source,
                        level,
                        schema_version,
                        redacted,
                        payload_json,
                        event_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event.run_id,
                        event.id,
                        event.event_type,
                        event.created_at,
                        event.source,
                        event.level,
                        event.schema_version,
                        1 if event.redacted else 0,
                        json.dumps(payload, ensure_ascii=False, sort_keys=True),
                        json.dumps(event_dict, ensure_ascii=False, sort_keys=True),
                    ),
                )
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        return sqlite3.connect(self.path)

    def _ensure_schema(self) -> None:
        connection = self._connect()
        try:
            with connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS events (
                        run_id TEXT NOT NULL,
                        id TEXT NOT NULL,
                        type TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        source TEXT NOT NULL,
                        level TEXT NOT NULL,
                        schema_version INTEGER NOT NULL,
                        redacted INTEGER NOT NULL,
                        payload_json TEXT NOT NULL,
                        event_json TEXT NOT NULL,
                        PRIMARY KEY (run_id, id)
                    )
                    """
                )
                connection.execute("CREATE INDEX IF NOT EXISTS idx_events_run_id ON events(run_id)")
                connection.execute("CREATE INDEX IF NOT EXISTS idx_events_type ON events(type)")
                connection.execute("CREATE INDEX IF NOT EXISTS idx_events_created_at ON events(created_at)")
        finally:
            connection.close()


class CompositeEventWriter:
    """把同一条事件写给多个 writer。

    典型组合:
        1. MemoryEventWriter: 保持当前进程内可查看 recentEvents。
        2. JsonlEventWriter: 追加到 data/events.jsonl,方便跨进程排障。

    生产原则:
        事件写入失败不能影响用户主流程。
        所以某个 writer 抛错时,这里只记录 warning,不继续向外抛。
    """

    def __init__(
        self,
        writers: list[EventWriter],
        warning_events: list[EventRecord] | None = None,
    ) -> None:
        self.writers = writers
        self.warning_events = warning_events

    def write(self, event: EventRecord) -> None:
        for writer in self.writers:
            try:
                writer.write(event)
            except Exception as error:  # pragma: no cover -具体异常由测试里的坏 writer 覆盖
                self._record_warning(event, writer, error)

    def _record_warning(
        self,
        event: EventRecord,
        writer: EventWriter,
        error: Exception,
    ) -> None:
        if self.warning_events is None:
            return
        self.warning_events.append(
            EventRecord(
                id=f"{event.id}_writer_warning",
                event_type="event_writer_warning",
                run_id=event.run_id,
                payload={
                    "failedEventId": event.id,
                    "writer": writer.__class__.__name__,
                    "error": str(error),
                },
                created_at=event.created_at,
                source="event_writer",
                level="warn",
                schema_version=event.schema_version,
                redacted=event.redacted,
            )
        )


def build_event_writer_from_env(events: list[EventRecord]) -> EventWriter:
    """根据环境变量创建事件 writer。

    默认只写内存,保持原行为不变。

    开启 JSONL:
        AGENTIC_EVENT_LOG=jsonl
        AGENTIC_EVENT_LOG_PATH=data/events.jsonl

    开启 SQLite:
        AGENTIC_EVENT_LOG=sqlite
        AGENTIC_EVENT_LOG_PATH=data/events.db
    """

    memory_writer = MemoryEventWriter(events)
    event_log_mode = os.getenv("AGENTIC_EVENT_LOG", "memory").lower()
    event_log_path = os.getenv("AGENTIC_EVENT_LOG_PATH")
    max_bytes = _optional_positive_int(os.getenv("AGENTIC_EVENT_LOG_MAX_BYTES"))
    backup_count = _optional_non_negative_int(os.getenv("AGENTIC_EVENT_LOG_BACKUP_COUNT"), default=3)
    use_lock = _optional_bool(os.getenv("AGENTIC_EVENT_LOG_LOCK"), default=True)
    if event_log_mode in {"sqlite", "db"}:
        return CompositeEventWriter(
            writers=[
                memory_writer,
                SQLiteEventWriter(event_log_path or "data/events.db"),
            ],
            warning_events=events,
        )
    if event_log_mode in {"1", "true", "jsonl"} or event_log_path:
        return CompositeEventWriter(
            writers=[
                memory_writer,
                JsonlEventWriter(
                    event_log_path or "data/events.jsonl",
                    max_bytes=max_bytes,
                    backup_count=backup_count,
                    use_lock=use_lock,
                ),
            ],
            warning_events=events,
        )
    return memory_writer


def redact_event(event: EventRecord) -> EventRecord:
    """返回脱敏后的事件副本。

    这里复用 MemoryPolicy 的 SENSITIVE_PATTERN,避免项目里出现两套敏感规则。
    """

    payload, redacted = redact_value(event.payload)
    if not isinstance(payload, dict):
        payload = {"value": payload}
    return replace(event, payload=payload, redacted=event.redacted or redacted)


def redact_value(value: Any, parent_key: str = "") -> tuple[Any, bool]:
    """递归脱敏任意 JSON-like 值。

    命中条件:
        - key 像 password/token/api key
        - 字符串 value 本身出现敏感词

    返回值是 (脱敏后的值, 是否发生过脱敏)。
    """

    if isinstance(value, dict):
        redacted_any = False
        output: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if _is_sensitive_text(key_text):
                output[key_text] = "[REDACTED]"
                redacted_any = True
                continue
            output_item, item_redacted = redact_value(item, key_text)
            output[key_text] = output_item
            redacted_any = redacted_any or item_redacted
        return output, redacted_any

    if isinstance(value, list):
        redacted_any = False
        output_list: list[Any] = []
        for item in value:
            output_item, item_redacted = redact_value(item, parent_key)
            output_list.append(output_item)
            redacted_any = redacted_any or item_redacted
        return output_list, redacted_any

    if isinstance(value, str) and (_is_sensitive_text(parent_key) or _is_sensitive_text(value)):
        return "[REDACTED]", True

    return value, False


def _is_sensitive_text(text: str) -> bool:
    return bool(SENSITIVE_PATTERN.search(text))


def _optional_positive_int(value: str | None, default: int | None = None) -> int | None:
    if value is None or not value.strip():
        return default
    try:
        parsed = int(value)
    except ValueError:
        return default
    return parsed if parsed > 0 else default


def _optional_non_negative_int(value: str | None, default: int = 0) -> int:
    if value is None or not value.strip():
        return default
    try:
        parsed = int(value)
    except ValueError:
        return default
    return parsed if parsed >= 0 else default


def _optional_bool(value: str | None, default: bool) -> bool:
    if value is None or not value.strip():
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default
