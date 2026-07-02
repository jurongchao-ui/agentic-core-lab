from __future__ import annotations

import json

from agentic_core.event_writer import (
    CompositeEventWriter,
    JsonlEventWriter,
    MemoryEventWriter,
    redact_event,
)
from agentic_core.memory import MemoryStore
from agentic_core.schemas import EventRecord


class FakeEventWriter:
    def __init__(self) -> None:
        self.events: list[EventRecord] = []

    def write(self, event: EventRecord) -> None:
        self.events.append(event)


class BrokenEventWriter:
    def write(self, event: EventRecord) -> None:
        raise OSError("disk full")


def test_default_memory_store_keeps_events_in_memory() -> None:
    memory = MemoryStore()

    event = memory.record_event(
        event_type="memory_decision",
        run_id="run_1",
        payload={"save": False},
    )

    assert event.id == "event_1"
    assert len(memory.events) == 1
    assert memory.events[0].event_type == "memory_decision"
    assert memory.events[0].source == "memory"
    assert memory.events[0].schema_version == 1
    assert memory.snapshot().recent_events == [event]


def test_memory_event_writer_appends_to_given_list() -> None:
    events: list[EventRecord] = []
    writer = MemoryEventWriter(events)
    event = EventRecord(
        id="event_1",
        event_type="run_started",
        run_id="run_1",
        payload={"goal": "hello"},
        created_at="2026-07-02T00:00:00+00:00",
    )

    writer.write(event)

    assert events == [event]


def test_memory_store_can_inject_fake_writer() -> None:
    writer = FakeEventWriter()
    memory = MemoryStore(event_writer=writer)

    first = memory.record_event(event_type="run_started", run_id="run_1", payload={})
    second = memory.record_event(event_type="run_completed", run_id="run_1", payload={})

    assert writer.events == [first, second]
    assert [event.id for event in writer.events] == ["event_1", "event_2"]
    assert memory.events == []


def test_record_event_keeps_legacy_dict_compatibility() -> None:
    memory = MemoryStore()

    event = memory.record_event(
        {
            "runId": "run_legacy",
            "type": "tool_observation",
            "step": 1,
            "ok": True,
        }
    )

    assert event.run_id == "run_legacy"
    assert event.event_type == "tool_observation"
    assert event.payload == {"step": 1, "ok": True}
    assert event.source == "tool"


def test_jsonl_event_writer_appends_one_json_object_per_line(tmp_path) -> None:
    path = tmp_path / "events.jsonl"
    writer = JsonlEventWriter(path)
    event = EventRecord(
        id="event_1",
        event_type="run_started",
        run_id="run_1",
        payload={"goal": "hello"},
        created_at="2026-07-02T00:00:00+00:00",
    )

    writer.write(event)
    writer.write(event)

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["runId"] == "run_1"
    assert json.loads(lines[0])["payload"] == {"goal": "hello"}
    assert (tmp_path / "events.jsonl.lock").exists()


def test_jsonl_event_writer_can_disable_lock_file(tmp_path) -> None:
    path = tmp_path / "events.jsonl"
    writer = JsonlEventWriter(path, use_lock=False)

    writer.write(event_record("event_1", {"goal": "hello"}))

    assert path.exists()
    assert not (tmp_path / "events.jsonl.lock").exists()


def test_jsonl_event_writer_rotates_by_size(tmp_path) -> None:
    path = tmp_path / "events.jsonl"
    writer = JsonlEventWriter(path, max_bytes=220, backup_count=2)

    writer.write(event_record("event_1", {"goal": "first"}))
    writer.write(event_record("event_2", {"goal": "second"}))
    writer.write(event_record("event_3", {"goal": "third"}))

    current = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    first_backup = [
        json.loads(line) for line in (tmp_path / "events.jsonl.1").read_text(encoding="utf-8").splitlines()
    ]
    second_backup = [
        json.loads(line) for line in (tmp_path / "events.jsonl.2").read_text(encoding="utf-8").splitlines()
    ]

    assert current[0]["id"] == "event_3"
    assert first_backup[0]["id"] == "event_2"
    assert second_backup[0]["id"] == "event_1"


def test_jsonl_event_writer_can_truncate_without_backups(tmp_path) -> None:
    path = tmp_path / "events.jsonl"
    writer = JsonlEventWriter(path, max_bytes=220, backup_count=0)

    writer.write(event_record("event_1", {"goal": "first"}))
    writer.write(event_record("event_2", {"goal": "second"}))

    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [line["id"] for line in lines] == ["event_2"]
    assert not (tmp_path / "events.jsonl.1").exists()


def test_composite_event_writer_records_warning_when_child_writer_fails() -> None:
    events: list[EventRecord] = []
    writer = CompositeEventWriter(
        writers=[MemoryEventWriter(events), BrokenEventWriter()],
        warning_events=events,
    )
    event = EventRecord(
        id="event_1",
        event_type="run_started",
        run_id="run_1",
        payload={},
        created_at="2026-07-02T00:00:00+00:00",
    )

    writer.write(event)

    assert events[0] == event
    assert events[1].event_type == "event_writer_warning"
    assert events[1].level == "warn"
    assert events[1].payload["writer"] == "BrokenEventWriter"


def test_memory_store_can_write_jsonl_from_environment(tmp_path, monkeypatch) -> None:
    path = tmp_path / "events.jsonl"
    monkeypatch.setenv("AGENTIC_EVENT_LOG", "jsonl")
    monkeypatch.setenv("AGENTIC_EVENT_LOG_PATH", str(path))
    memory = MemoryStore()

    event = memory.record_event(event_type="run_started", run_id="run_1", payload={"goal": "hello"})

    assert memory.events == [event]
    saved_event = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert saved_event["id"] == "event_1"
    assert saved_event["type"] == "run_started"
    assert saved_event["schemaVersion"] == 1


def test_memory_store_can_configure_jsonl_rotation_from_environment(tmp_path, monkeypatch) -> None:
    path = tmp_path / "events.jsonl"
    monkeypatch.setenv("AGENTIC_EVENT_LOG", "jsonl")
    monkeypatch.setenv("AGENTIC_EVENT_LOG_PATH", str(path))
    monkeypatch.setenv("AGENTIC_EVENT_LOG_MAX_BYTES", "220")
    monkeypatch.setenv("AGENTIC_EVENT_LOG_BACKUP_COUNT", "1")
    memory = MemoryStore()

    memory.record_event(event_type="run_started", run_id="run_1", payload={"goal": "first"})
    memory.record_event(event_type="run_started", run_id="run_2", payload={"goal": "second"})

    assert path.exists()
    assert (tmp_path / "events.jsonl.1").exists()


def test_memory_store_can_disable_jsonl_lock_from_environment(tmp_path, monkeypatch) -> None:
    path = tmp_path / "events.jsonl"
    monkeypatch.setenv("AGENTIC_EVENT_LOG", "jsonl")
    monkeypatch.setenv("AGENTIC_EVENT_LOG_PATH", str(path))
    monkeypatch.setenv("AGENTIC_EVENT_LOG_LOCK", "0")
    memory = MemoryStore()

    memory.record_event(event_type="run_started", run_id="run_1", payload={"goal": "hello"})

    assert path.exists()
    assert not (tmp_path / "events.jsonl.lock").exists()


def test_event_payload_is_redacted_before_writing() -> None:
    memory = MemoryStore()

    event = memory.record_event(
        event_type="run_started",
        run_id="run_1",
        payload={
            "goal": "请记住我的密码是 123456",
            "nested": {"api_key": "sk-test"},
        },
    )

    assert event.redacted is True
    assert event.payload["goal"] == "[REDACTED]"
    assert event.payload["nested"] == {"api_key": "[REDACTED]"}
    assert memory.events[0] == event


def test_redact_event_keeps_safe_payload() -> None:
    event = EventRecord(
        id="event_1",
        event_type="run_started",
        run_id="run_1",
        payload={"goal": "安排 agentic 学习计划"},
        created_at="2026-07-02T00:00:00+00:00",
    )

    redacted = redact_event(event)

    assert redacted.payload == event.payload
    assert redacted.redacted is False


def event_record(event_id: str, payload: dict) -> EventRecord:
    return EventRecord(
        id=event_id,
        event_type="run_started",
        run_id="run_1",
        payload=payload,
        created_at="2026-07-02T00:00:00+00:00",
    )
