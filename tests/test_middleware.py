from __future__ import annotations

import time
import json

from agentic_core.runtime.agent import Agent
from agentic_core.runtime.context import RuntimeIdentity
from agentic_core.memory.store import MemoryStore
from agentic_core.policies.memory import RuleBasedMemoryPolicy
from agentic_core.tools.middleware import (
    ApprovalMiddleware,
    CostAccountingMiddleware,
    InMemoryToolTraceSink,
    JsonFileIdempotencyStore,
    JsonFileToolBudgetStore,
    JsonlToolTraceSink,
    MiddlewarePipeline,
    OtlpHttpToolTraceSink,
    SQLiteIdempotencyStore,
    SQLiteToolBudgetStore,
    ToolCallContext,
    ToolGovernanceMiddleware,
    ToolGovernancePolicy,
    build_middleware_pipeline_from_env,
)
from agentic_core.policies.planner import RuleBasedPlanner
from agentic_core.runtime.schemas import Action
from agentic_core.tools.registry import ToolRegistry, ToolSpec


def test_cost_accounting_middleware_records_tool_cost() -> None:
    context = ToolCallContext(
        run_id="run_1",
        step=1,
        action=Action.tool("expensive.tool", {}),
        tool=ToolSpec(
            name="expensive.tool",
            description="test",
            execute=lambda data: data,
            input_schema={},
            side_effect="read",
            cost_units=7,
        ),
    )

    observation = CostAccountingMiddleware().before_tool(context)

    assert observation is None
    assert context.metadata["costUnits"] == 7


def test_approval_middleware_blocks_unapproved_tool() -> None:
    context = ToolCallContext(
        run_id="run_1",
        step=1,
        action=Action.tool("danger.delete", {}),
        tool=ToolSpec(
            name="danger.delete",
            description="danger",
            execute=lambda data: data,
            input_schema={},
            side_effect="write",
            requires_approval=True,
        ),
    )

    observation = ApprovalMiddleware().before_tool(context)

    assert observation is not None
    assert observation.ok is False
    assert "requires approval" in str(observation.error)


def test_agent_blocks_tool_that_requires_approval() -> None:
    memory = MemoryStore()
    policy = RuleBasedMemoryPolicy()
    tools = ToolRegistry(memory, policy)
    executed = {"value": False}

    def execute_danger(input_data: dict) -> dict:
        executed["value"] = True
        return {"deleted": True}

    tools._register(
        "danger.delete",
        "Dangerous write.",
        execute_danger,
        side_effect="write",
        requires_approval=True,
        risk_level="high",
    )
    agent = Agent(
        planner=DangerPlanner(),
        tools=tools,
        memory=memory,
        memory_policy=policy,
        responder=None,
    )

    result = agent.run_typed("删除全部数据")

    assert executed["value"] is False
    assert result.trace[0].action.tool_name == "danger.delete"
    assert result.trace[0].observation.ok is False
    assert "requires approval" in str(result.trace[0].observation.error)
    assert result.trace[0].observation.metadata["shortCircuited"] is True
    assert result.trace[0].observation.metadata["requiresApproval"] is True
    assert "执行失败" in result.answer


def test_empty_middleware_pipeline_keeps_tool_execution_unchanged() -> None:
    memory = MemoryStore()
    policy = RuleBasedMemoryPolicy()
    agent = Agent(
        planner=RuleBasedPlanner(),
        tools=ToolRegistry(memory, policy),
        memory=memory,
        memory_policy=policy,
        middleware_pipeline=MiddlewarePipeline([]),
    )

    result = agent.run_typed("帮我计算 128 * 7")

    assert result.trace[0].observation.ok is True
    assert result.trace[0].observation.output["result"] == 896
    assert result.trace[0].observation.metadata["toolName"] == "calculator"


def test_pipeline_retries_failed_tool_using_tool_metadata() -> None:
    context = ToolCallContext(
        run_id="run_1",
        step=1,
        action=Action.tool("flaky.tool", {}),
        tool=ToolSpec(
            name="flaky.tool",
            description="flaky",
            execute=lambda data: data,
            input_schema={},
            side_effect="read",
            retry_count=1,
        ),
    )
    calls = {"count": 0}

    def execute() -> dict:
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("temporary failure")
        return {"ok": True}

    observation = MiddlewarePipeline.default().execute_tool(context, execute)

    assert observation.ok is True
    assert observation.output == {"ok": True}
    assert calls["count"] == 2
    assert observation.metadata["attempts"] == 2
    assert observation.metadata["retryCount"] == 1


def test_pipeline_times_out_tool_using_tool_metadata() -> None:
    context = ToolCallContext(
        run_id="run_1",
        step=1,
        action=Action.tool("slow.tool", {}),
        tool=ToolSpec(
            name="slow.tool",
            description="slow",
            execute=lambda data: data,
            input_schema={},
            side_effect="read",
            timeout_ms=1,
        ),
    )

    def execute() -> dict:
        time.sleep(0.05)
        return {"done": True}

    observation = MiddlewarePipeline.default().execute_tool(context, execute)

    assert observation.ok is False
    assert "timed out" in str(observation.error)
    assert observation.metadata["timeoutMs"] == 1
    assert observation.metadata["attempts"] == 1


def test_pipeline_generates_stable_idempotency_key() -> None:
    tool = ToolSpec(
        name="note.add",
        description="write",
        execute=lambda data: data,
        input_schema={},
        side_effect="write",
        version="1.0",
    )
    first_context = ToolCallContext(
        run_id="run_1",
        step=1,
        action=Action.tool("note.add", {"text": "hello"}),
        tool=tool,
    )
    second_context = ToolCallContext(
        run_id="run_1",
        step=1,
        action=Action.tool("note.add", {"text": "hello"}),
        tool=tool,
    )

    first = MiddlewarePipeline.default().execute_tool(first_context, lambda: {"saved": True})
    second = MiddlewarePipeline.default().execute_tool(second_context, lambda: {"saved": True})

    assert first.metadata["idempotencyKey"] == second.metadata["idempotencyKey"]
    assert str(first.metadata["idempotencyKey"]).startswith("tool_")


def test_pipeline_uses_idempotency_store_for_successful_write_tool() -> None:
    tool = ToolSpec(
        name="note.add",
        description="write",
        execute=lambda data: data,
        input_schema={},
        side_effect="write",
        version="1.0",
    )
    pipeline = MiddlewarePipeline.default()
    calls = {"count": 0}

    def execute() -> dict[str, int]:
        calls["count"] += 1
        return {"saved": calls["count"]}

    first_context = ToolCallContext(
        run_id="run_1",
        step=1,
        action=Action.tool("note.add", {"text": "hello"}),
        tool=tool,
    )
    second_context = ToolCallContext(
        run_id="run_1",
        step=1,
        action=Action.tool("note.add", {"text": "hello"}),
        tool=tool,
    )

    first = pipeline.execute_tool(first_context, execute)
    second = pipeline.execute_tool(second_context, execute)

    assert calls["count"] == 1
    assert first.output == {"saved": 1}
    assert second.output == {"saved": 1}
    assert first.metadata["idempotencyHit"] is False
    assert first.metadata["idempotencyStored"] is True
    assert second.metadata["idempotencyHit"] is True
    assert second.metadata["shortCircuited"] is True
    assert second.metadata["attempts"] == 0


def test_pipeline_does_not_cache_read_tools() -> None:
    tool = ToolSpec(
        name="todo.list",
        description="read",
        execute=lambda data: data,
        input_schema={},
        side_effect="read",
        version="1.0",
    )
    pipeline = MiddlewarePipeline.default()
    calls = {"count": 0}

    def execute() -> dict[str, int]:
        calls["count"] += 1
        return {"count": calls["count"]}

    first_context = ToolCallContext(
        run_id="run_1",
        step=1,
        action=Action.tool("todo.list", {}),
        tool=tool,
    )
    second_context = ToolCallContext(
        run_id="run_1",
        step=1,
        action=Action.tool("todo.list", {}),
        tool=tool,
    )

    first = pipeline.execute_tool(first_context, execute)
    second = pipeline.execute_tool(second_context, execute)

    assert calls["count"] == 2
    assert first.output == {"count": 1}
    assert second.output == {"count": 2}
    assert first.metadata["idempotencyHit"] is False
    assert second.metadata["idempotencyHit"] is False


def test_pipeline_idempotency_key_is_scoped_by_identity() -> None:
    tool = ToolSpec(
        name="note.add",
        description="write",
        execute=lambda data: data,
        input_schema={},
        side_effect="write",
        version="1.0",
    )
    pipeline = MiddlewarePipeline.default()
    calls = {"count": 0}

    def execute() -> dict[str, int]:
        calls["count"] += 1
        return {"saved": calls["count"]}

    first_context = ToolCallContext(
        run_id="run_1",
        step=1,
        action=Action.tool("note.add", {"text": "hello"}),
        tool=tool,
        identity=RuntimeIdentity(user_id="user_a", tenant_id="tenant_1"),
    )
    second_context = ToolCallContext(
        run_id="run_1",
        step=1,
        action=Action.tool("note.add", {"text": "hello"}),
        tool=tool,
        identity=RuntimeIdentity(user_id="user_b", tenant_id="tenant_1"),
    )

    first = pipeline.execute_tool(first_context, execute)
    second = pipeline.execute_tool(second_context, execute)

    assert calls["count"] == 2
    assert first.metadata["idempotencyKey"] != second.metadata["idempotencyKey"]
    assert first.metadata["idempotencyHit"] is False
    assert second.metadata["idempotencyHit"] is False


def test_pipeline_does_not_cache_failed_write_tool() -> None:
    tool = ToolSpec(
        name="note.add",
        description="write",
        execute=lambda data: data,
        input_schema={},
        side_effect="write",
        version="1.0",
    )
    pipeline = MiddlewarePipeline.default()
    calls = {"count": 0}

    def execute() -> dict[str, int]:
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("temporary failure")
        return {"saved": calls["count"]}

    first_context = ToolCallContext(
        run_id="run_1",
        step=1,
        action=Action.tool("note.add", {"text": "hello"}),
        tool=tool,
    )
    second_context = ToolCallContext(
        run_id="run_1",
        step=1,
        action=Action.tool("note.add", {"text": "hello"}),
        tool=tool,
    )

    first = pipeline.execute_tool(first_context, execute)
    second = pipeline.execute_tool(second_context, execute)

    assert calls["count"] == 2
    assert first.ok is False
    assert second.ok is True
    assert second.output == {"saved": 2}
    assert first.metadata["idempotencyStored"] is False
    assert second.metadata["idempotencyHit"] is False
    assert second.metadata["idempotencyStored"] is True


def test_pipeline_redacts_sensitive_tool_output() -> None:
    context = ToolCallContext(
        run_id="run_1",
        step=1,
        action=Action.tool("secret.tool", {}),
        tool=ToolSpec(
            name="secret.tool",
            description="returns sensitive output",
            execute=lambda data: data,
            input_schema={},
            side_effect="read",
        ),
    )

    observation = MiddlewarePipeline.default().execute_tool(
        context,
        lambda: {"token": "abc123", "safe": "hello", "nested": {"value": "密码是 123456"}},
    )

    assert observation.ok is True
    assert observation.output == {
        "token": "[REDACTED]",
        "safe": "hello",
        "nested": {"value": "[REDACTED]"},
    }
    assert observation.metadata["toolOutputSafety"]["redacted"] is True
    assert observation.metadata["toolOutputSafety"]["fields"] == ["output.token", "output.nested.value"]


def test_pipeline_redacts_sensitive_tool_error() -> None:
    context = ToolCallContext(
        run_id="run_1",
        step=1,
        action=Action.tool("secret.tool", {}),
        tool=ToolSpec(
            name="secret.tool",
            description="raises sensitive error",
            execute=lambda data: data,
            input_schema={},
            side_effect="read",
        ),
    )

    observation = MiddlewarePipeline.default().execute_tool(
        context,
        lambda: (_ for _ in ()).throw(RuntimeError("token abc123 rejected")),
    )

    assert observation.ok is False
    assert observation.error == "[REDACTED]"
    assert observation.metadata["toolOutputSafety"]["redacted"] is True
    assert observation.metadata["toolOutputSafety"]["fields"] == ["error"]


def test_pipeline_records_tool_trace_span_for_successful_tool() -> None:
    trace_sink = InMemoryToolTraceSink()
    context = ToolCallContext(
        run_id="run_1",
        step=1,
        action=Action.tool("calculator", {"expression": "128 * 7"}),
        tool=ToolSpec(
            name="calculator",
            description="calculate",
            execute=lambda data: data,
            input_schema={},
            side_effect="read",
            version="1.0",
        ),
    )

    observation = MiddlewarePipeline.default(trace_sink=trace_sink).execute_tool(
        context,
        lambda: {"result": 896},
    )

    assert observation.ok is True
    assert observation.metadata["toolTraceSpanId"] == trace_sink.spans[0].span_id
    assert observation.metadata["traceSink"] == "InMemoryToolTraceSink"
    assert len(trace_sink.spans) == 1
    span = trace_sink.spans[0]
    assert span.trace_id == "run_1"
    assert span.name == "tool.calculator"
    assert span.status == "ok"
    assert span.attributes["toolName"] == "calculator"
    assert span.attributes["sideEffect"] == "read"
    assert "output" not in span.to_dict()


def test_pipeline_records_tool_trace_span_for_short_circuit() -> None:
    trace_sink = InMemoryToolTraceSink()
    context = ToolCallContext(
        run_id="run_1",
        step=1,
        action=Action.tool("danger.delete", {}),
        tool=ToolSpec(
            name="danger.delete",
            description="danger",
            execute=lambda data: data,
            input_schema={},
            side_effect="write",
            risk_level="high",
        ),
    )

    observation = MiddlewarePipeline.default(trace_sink=trace_sink).execute_tool(
        context,
        lambda: {"deleted": True},
    )

    assert observation.ok is False
    assert len(trace_sink.spans) == 1
    span = trace_sink.spans[0]
    assert span.status == "error"
    assert span.error is not None
    assert span.attributes["shortCircuited"] is True
    assert span.attributes["approvalRequired"] is True
    assert span.attributes["riskLevel"] == "high"


def test_jsonl_tool_trace_sink_persists_span_without_tool_output(tmp_path) -> None:
    path = tmp_path / "tool-spans.jsonl"
    context = ToolCallContext(
        run_id="run_1",
        step=1,
        action=Action.tool("secret.tool", {}),
        tool=ToolSpec(
            name="secret.tool",
            description="secret",
            execute=lambda data: data,
            input_schema={},
            side_effect="read",
        ),
    )

    observation = MiddlewarePipeline.default(
        trace_sink=JsonlToolTraceSink(path, use_lock=False),
    ).execute_tool(context, lambda: {"token": "secret-value"})

    assert observation.ok is True
    assert path.exists()
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    data = json.loads(lines[0])
    assert data["traceId"] == "run_1"
    assert data["status"] == "ok"
    assert data["attributes"]["toolName"] == "secret.tool"
    assert "output" not in data
    assert "secret-value" not in lines[0]


def test_build_middleware_pipeline_from_env_uses_jsonl_trace_sink(tmp_path, monkeypatch) -> None:
    path = tmp_path / "tool-spans.jsonl"
    monkeypatch.setenv("AGENTIC_TOOL_TRACE_SINK", "jsonl")
    monkeypatch.setenv("AGENTIC_TOOL_TRACE_PATH", str(path))
    monkeypatch.setenv("AGENTIC_TOOL_TRACE_LOCK", "0")
    context = ToolCallContext(
        run_id="run_1",
        step=1,
        action=Action.tool("calculator", {}),
        tool=ToolSpec(
            name="calculator",
            description="calculate",
            execute=lambda data: data,
            input_schema={},
            side_effect="read",
        ),
    )

    observation = build_middleware_pipeline_from_env().execute_tool(context, lambda: {"result": 1})

    assert observation.ok is True
    assert observation.metadata["traceSink"] == "JsonlToolTraceSink"
    assert path.exists()


def test_otlp_http_tool_trace_sink_exports_otlp_payload(monkeypatch) -> None:
    sent = _capture_urlopen(monkeypatch)
    context = ToolCallContext(
        run_id="run_1",
        step=1,
        action=Action.tool("calculator", {}),
        tool=ToolSpec(
            name="calculator",
            description="calculate",
            execute=lambda data: data,
            input_schema={},
            side_effect="read",
        )
    )

    observation = MiddlewarePipeline.default(
        trace_sink=OtlpHttpToolTraceSink(
            endpoint="http://collector.example/v1/traces",
            service_name="agentic-test",
            deployment_environment="test",
        )
    ).execute_tool(context, lambda: {"result": 1})

    assert observation.ok is True
    assert observation.metadata["traceSink"] == "OtlpHttpToolTraceSink"
    assert len(sent) == 1
    assert sent[0]["url"] == "http://collector.example/v1/traces"
    payload = sent[0]["payload"]
    resource_span = payload["resourceSpans"][0]
    resource_attrs = _otlp_attrs_to_dict(resource_span["resource"]["attributes"])
    span = resource_span["scopeSpans"][0]["spans"][0]
    span_attrs = _otlp_attrs_to_dict(span["attributes"])
    assert resource_attrs["service.name"]["stringValue"] == "agentic-test"
    assert resource_attrs["deployment.environment"]["stringValue"] == "test"
    assert span["name"] == "tool.calculator"
    assert span["traceId"] != "run_1"
    assert len(span["traceId"]) == 32
    assert len(span["spanId"]) == 16
    assert span["status"]["code"] == "STATUS_CODE_OK"
    assert span_attrs["toolName"]["stringValue"] == "calculator"
    assert "output" not in span_attrs


def test_build_middleware_pipeline_from_env_uses_otlp_http_trace_sink(monkeypatch) -> None:
    sent = _capture_urlopen(monkeypatch)
    monkeypatch.setenv("AGENTIC_TOOL_TRACE_SINK", "otlp_http")
    monkeypatch.setenv("AGENTIC_TOOL_TRACE_ENDPOINT", "http://collector.example/v1/traces")
    monkeypatch.setenv("AGENTIC_TOOL_TRACE_TIMEOUT_MS", "1000")
    monkeypatch.setenv("AGENTIC_SERVICE_NAME", "agentic-env-test")
    monkeypatch.setenv("AGENTIC_DEPLOYMENT_ENVIRONMENT", "test")
    monkeypatch.setenv("AGENTIC_TOOL_TRACE_HEADERS", '{"X-Test-Trace":"yes"}')
    context = ToolCallContext(
        run_id="run_1",
        step=1,
        action=Action.tool("calculator", {}),
        tool=ToolSpec(
            name="calculator",
            description="calculate",
            execute=lambda data: data,
            input_schema={},
            side_effect="read",
        ),
    )

    observation = build_middleware_pipeline_from_env().execute_tool(context, lambda: {"result": 1})

    assert observation.ok is True
    assert observation.metadata["traceSink"] == "OtlpHttpToolTraceSink"
    assert sent[0]["headers"]["X-test-trace"] == "yes"
    assert sent[0]["payload"]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["name"] == "tool.calculator"


def test_pipeline_keeps_tool_success_when_trace_sink_fails() -> None:
    context = ToolCallContext(
        run_id="run_1",
        step=1,
        action=Action.tool("calculator", {}),
        tool=ToolSpec(
            name="calculator",
            description="calculate",
            execute=lambda data: data,
            input_schema={},
            side_effect="read",
        ),
    )

    observation = MiddlewarePipeline.default(trace_sink=FailingTraceSink()).execute_tool(
        context,
        lambda: {"result": 1},
    )

    assert observation.ok is True
    assert observation.output == {"result": 1}
    assert "trace backend unavailable" in str(observation.metadata["toolTraceError"])


def test_pipeline_stores_redacted_output_in_idempotency_cache() -> None:
    tool = ToolSpec(
        name="note.add",
        description="write",
        execute=lambda data: data,
        input_schema={},
        side_effect="write",
        version="1.0",
    )
    pipeline = MiddlewarePipeline.default()
    calls = {"count": 0}

    def execute() -> dict[str, str]:
        calls["count"] += 1
        return {"text": "safe", "api_key": "secret-value"}

    first_context = ToolCallContext(
        run_id="run_1",
        step=1,
        action=Action.tool("note.add", {"text": "safe"}),
        tool=tool,
    )
    second_context = ToolCallContext(
        run_id="run_1",
        step=1,
        action=Action.tool("note.add", {"text": "safe"}),
        tool=tool,
    )

    first = pipeline.execute_tool(first_context, execute)
    second = pipeline.execute_tool(second_context, execute)

    assert calls["count"] == 1
    assert first.output["api_key"] == "[REDACTED]"
    assert second.output["api_key"] == "[REDACTED]"
    assert second.metadata["idempotencyHit"] is True


def test_json_file_idempotency_store_shares_write_results_across_pipeline_instances(tmp_path) -> None:
    path = tmp_path / "tool-idempotency.json"
    tool = ToolSpec(
        name="note.add",
        description="write",
        execute=lambda data: data,
        input_schema={},
        side_effect="write",
        version="1.0",
    )
    calls = {"count": 0}

    def execute() -> dict[str, str]:
        calls["count"] += 1
        return {"text": f"saved-{calls['count']}", "token": "secret-value"}

    first_context = ToolCallContext(
        run_id="run_1",
        step=1,
        action=Action.tool("note.add", {"text": "safe"}),
        tool=tool,
    )
    second_context = ToolCallContext(
        run_id="run_1",
        step=1,
        action=Action.tool("note.add", {"text": "safe"}),
        tool=tool,
    )

    first = MiddlewarePipeline.default(
        idempotency_store=JsonFileIdempotencyStore(path),
    ).execute_tool(first_context, execute)
    second = MiddlewarePipeline.default(
        idempotency_store=JsonFileIdempotencyStore(path),
    ).execute_tool(second_context, execute)

    assert calls["count"] == 1
    assert first.output == {"text": "saved-1", "token": "[REDACTED]"}
    assert second.output == {"text": "saved-1", "token": "[REDACTED]"}
    assert second.metadata["idempotencyHit"] is True
    assert second.metadata["idempotencyStore"] == "JsonFileIdempotencyStore"
    data = json.loads(path.read_text(encoding="utf-8"))
    saved_observations = list(data["observations"].values())
    assert saved_observations[0]["output"]["token"] == "[REDACTED]"


def test_build_middleware_pipeline_from_env_uses_json_idempotency_store(tmp_path, monkeypatch) -> None:
    path = tmp_path / "tool-idempotency.json"
    monkeypatch.setenv("AGENTIC_IDEMPOTENCY_STORE", "json")
    monkeypatch.setenv("AGENTIC_IDEMPOTENCY_PATH", str(path))
    monkeypatch.setenv("AGENTIC_IDEMPOTENCY_LOCK", "0")
    tool = ToolSpec(
        name="note.add",
        description="write",
        execute=lambda data: data,
        input_schema={},
        side_effect="write",
        version="1.0",
    )
    first_context = ToolCallContext(
        run_id="run_1",
        step=1,
        action=Action.tool("note.add", {"text": "safe"}),
        tool=tool,
    )
    second_context = ToolCallContext(
        run_id="run_1",
        step=1,
        action=Action.tool("note.add", {"text": "safe"}),
        tool=tool,
    )
    first_pipeline = build_middleware_pipeline_from_env()
    second_pipeline = build_middleware_pipeline_from_env()
    calls = {"count": 0}

    def execute() -> dict[str, int]:
        calls["count"] += 1
        return {"count": calls["count"]}

    first = first_pipeline.execute_tool(first_context, execute)
    second = second_pipeline.execute_tool(second_context, execute)

    assert first.ok is True
    assert calls["count"] == 1
    assert second.metadata["idempotencyHit"] is True
    assert second.metadata["idempotencyStore"] == "JsonFileIdempotencyStore"
    assert path.exists()


def test_sqlite_idempotency_store_shares_write_results_across_pipeline_instances(tmp_path) -> None:
    path = tmp_path / "tool-runtime.db"
    tool = ToolSpec(
        name="note.add",
        description="write",
        execute=lambda data: data,
        input_schema={},
        side_effect="write",
        version="1.0",
    )
    calls = {"count": 0}

    def execute() -> dict[str, str]:
        calls["count"] += 1
        return {"text": f"saved-{calls['count']}", "token": "secret-value"}

    first_context = ToolCallContext(
        run_id="run_1",
        step=1,
        action=Action.tool("note.add", {"text": "safe"}),
        tool=tool,
    )
    second_context = ToolCallContext(
        run_id="run_1",
        step=1,
        action=Action.tool("note.add", {"text": "safe"}),
        tool=tool,
    )

    first = MiddlewarePipeline.default(
        idempotency_store=SQLiteIdempotencyStore(path),
    ).execute_tool(first_context, execute)
    second = MiddlewarePipeline.default(
        idempotency_store=SQLiteIdempotencyStore(path),
    ).execute_tool(second_context, execute)

    assert calls["count"] == 1
    assert first.output == {"text": "saved-1", "token": "[REDACTED]"}
    assert second.output == {"text": "saved-1", "token": "[REDACTED]"}
    assert second.metadata["idempotencyHit"] is True
    assert second.metadata["idempotencyStore"] == "SQLiteIdempotencyStore"


def test_build_middleware_pipeline_from_env_uses_sqlite_idempotency_store(tmp_path, monkeypatch) -> None:
    path = tmp_path / "tool-runtime.db"
    monkeypatch.setenv("AGENTIC_IDEMPOTENCY_STORE", "sqlite")
    monkeypatch.setenv("AGENTIC_IDEMPOTENCY_PATH", str(path))
    tool = ToolSpec(
        name="note.add",
        description="write",
        execute=lambda data: data,
        input_schema={},
        side_effect="write",
        version="1.0",
    )
    first_context = ToolCallContext(
        run_id="run_1",
        step=1,
        action=Action.tool("note.add", {"text": "safe"}),
        tool=tool,
    )
    second_context = ToolCallContext(
        run_id="run_1",
        step=1,
        action=Action.tool("note.add", {"text": "safe"}),
        tool=tool,
    )
    first_pipeline = build_middleware_pipeline_from_env()
    second_pipeline = build_middleware_pipeline_from_env()
    calls = {"count": 0}

    def execute() -> dict[str, int]:
        calls["count"] += 1
        return {"count": calls["count"]}

    first = first_pipeline.execute_tool(first_context, execute)
    second = second_pipeline.execute_tool(second_context, execute)

    assert first.ok is True
    assert calls["count"] == 1
    assert second.metadata["idempotencyHit"] is True
    assert second.metadata["idempotencyStore"] == "SQLiteIdempotencyStore"
    assert path.exists()


def test_governance_middleware_denies_disallowed_permission_scope() -> None:
    context = ToolCallContext(
        run_id="run_1",
        step=1,
        action=Action.tool("note.add", {"text": "hello"}),
        tool=ToolSpec(
            name="note.add",
            description="write note",
            execute=lambda data: data,
            input_schema={},
            side_effect="write",
            permission_scope="memory:note:write",
        ),
    )
    middleware = ToolGovernanceMiddleware(
        ToolGovernancePolicy(allowed_permission_scopes={"tool:calculator:read"})
    )

    observation = middleware.before_tool(context)

    assert observation is not None
    assert observation.ok is False
    assert "permission scope not allowed" in str(observation.error)


def test_governance_middleware_blocks_denied_permission_scope() -> None:
    context = ToolCallContext(
        run_id="run_1",
        step=1,
        action=Action.tool("todo.list", {}),
        tool=ToolSpec(
            name="todo.list",
            description="list todo",
            execute=lambda data: data,
            input_schema={},
            side_effect="read",
            permission_scope="memory:todo:read",
        ),
    )
    middleware = ToolGovernanceMiddleware(
        ToolGovernancePolicy(denied_permission_scopes={"memory:todo:read"})
    )

    observation = middleware.before_tool(context)

    assert observation is not None
    assert "permission scope denied" in str(observation.error)


def test_default_pipeline_requires_approval_for_high_risk_tool() -> None:
    context = ToolCallContext(
        run_id="run_1",
        step=1,
        action=Action.tool("danger.delete", {}),
        tool=ToolSpec(
            name="danger.delete",
            description="danger",
            execute=lambda data: data,
            input_schema={},
            side_effect="write",
            risk_level="high",
            requires_approval=False,
        ),
    )
    executed = {"value": False}

    def execute() -> dict:
        executed["value"] = True
        return {"deleted": True}

    observation = MiddlewarePipeline.default().execute_tool(context, execute)

    assert executed["value"] is False
    assert observation.ok is False
    assert "risk level high requires approval" in str(observation.error)
    assert observation.metadata["approvalRequired"] is True
    assert observation.metadata["shortCircuited"] is True


def test_governance_middleware_can_require_approval_for_write_side_effect() -> None:
    context = ToolCallContext(
        run_id="run_1",
        step=1,
        action=Action.tool("note.add", {"text": "hello"}),
        tool=ToolSpec(
            name="note.add",
            description="write note",
            execute=lambda data: data,
            input_schema={},
            side_effect="write",
            risk_level="medium",
        ),
    )
    middleware = ToolGovernanceMiddleware(
        ToolGovernancePolicy(require_approval_for_side_effects={"write"})
    )

    observation = middleware.before_tool(context)

    assert observation is not None
    assert observation.ok is False
    assert "side effect write requires approval" in str(observation.error)


def test_governance_budget_limits_cost_units_per_run() -> None:
    middleware = ToolGovernanceMiddleware(ToolGovernancePolicy(max_cost_units_per_run=3))
    tool = ToolSpec(
        name="expensive.tool",
        description="expensive",
        execute=lambda data: data,
        input_schema={},
        side_effect="read",
        cost_units=2,
    )
    first = ToolCallContext(run_id="run_1", step=1, action=Action.tool("expensive.tool", {}), tool=tool)
    second = ToolCallContext(run_id="run_1", step=2, action=Action.tool("expensive.tool", {}), tool=tool)

    first_observation = middleware.before_tool(first)
    second_observation = middleware.before_tool(second)

    assert first_observation is None
    assert first.metadata["budgetUsedAfter"] == 2
    assert second_observation is not None
    assert "tool budget exceeded" in str(second_observation.error)
    assert second.metadata["budgetUsedBefore"] == 2
    assert second.metadata["budgetUsedAfter"] == 4


def test_json_file_budget_store_shares_budget_across_middleware_instances(tmp_path) -> None:
    path = tmp_path / "tool-budgets.json"
    policy = ToolGovernancePolicy(max_cost_units_per_run=3)
    tool = ToolSpec(
        name="expensive.tool",
        description="expensive",
        execute=lambda data: data,
        input_schema={},
        side_effect="read",
        cost_units=2,
    )
    first = ToolCallContext(run_id="run_1", step=1, action=Action.tool("expensive.tool", {}), tool=tool)
    second = ToolCallContext(run_id="run_1", step=2, action=Action.tool("expensive.tool", {}), tool=tool)

    first_observation = ToolGovernanceMiddleware(
        policy,
        budget_store=JsonFileToolBudgetStore(path),
    ).before_tool(first)
    second_observation = ToolGovernanceMiddleware(
        policy,
        budget_store=JsonFileToolBudgetStore(path),
    ).before_tool(second)

    assert first_observation is None
    assert first.metadata["budgetStore"] == "JsonFileToolBudgetStore"
    assert second_observation is not None
    assert "tool budget exceeded" in str(second_observation.error)
    assert second.metadata["budgetUsedBefore"] == 2
    assert second.metadata["budgetUsedAfter"] == 4
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["budgets"] == {"default_tenant:run_1": 2}


def test_build_middleware_pipeline_from_env_uses_json_budget_store(tmp_path, monkeypatch) -> None:
    path = tmp_path / "tool-budgets.json"
    monkeypatch.setenv("AGENTIC_TOOL_BUDGET_STORE", "json")
    monkeypatch.setenv("AGENTIC_TOOL_BUDGET_PATH", str(path))
    monkeypatch.setenv("AGENTIC_TOOL_BUDGET_LOCK", "0")
    tool = ToolSpec(
        name="expensive.tool",
        description="expensive",
        execute=lambda data: data,
        input_schema={},
        side_effect="read",
        cost_units=1,
    )
    context = ToolCallContext(run_id="run_1", step=1, action=Action.tool("expensive.tool", {}), tool=tool)
    pipeline = build_middleware_pipeline_from_env()
    governance = pipeline.middlewares[0]
    assert isinstance(governance, ToolGovernanceMiddleware)
    governance.policy.max_cost_units_per_run = 3

    observation = pipeline.execute_tool(context, lambda: {"ok": True})

    assert observation.ok is True
    assert observation.metadata["budgetStore"] == "JsonFileToolBudgetStore"
    assert path.exists()


def test_sqlite_budget_store_shares_budget_across_middleware_instances(tmp_path) -> None:
    path = tmp_path / "tool-runtime.db"
    policy = ToolGovernancePolicy(max_cost_units_per_run=3)
    tool = ToolSpec(
        name="expensive.tool",
        description="expensive",
        execute=lambda data: data,
        input_schema={},
        side_effect="read",
        cost_units=2,
    )
    first = ToolCallContext(run_id="run_1", step=1, action=Action.tool("expensive.tool", {}), tool=tool)
    second = ToolCallContext(run_id="run_1", step=2, action=Action.tool("expensive.tool", {}), tool=tool)

    first_observation = ToolGovernanceMiddleware(
        policy,
        budget_store=SQLiteToolBudgetStore(path),
    ).before_tool(first)
    second_observation = ToolGovernanceMiddleware(
        policy,
        budget_store=SQLiteToolBudgetStore(path),
    ).before_tool(second)

    assert first_observation is None
    assert first.metadata["budgetStore"] == "SQLiteToolBudgetStore"
    assert second_observation is not None
    assert "tool budget exceeded" in str(second_observation.error)
    assert second.metadata["budgetUsedBefore"] == 2
    assert second.metadata["budgetUsedAfter"] == 4
    assert path.exists()


def test_build_middleware_pipeline_from_env_uses_sqlite_budget_store(tmp_path, monkeypatch) -> None:
    path = tmp_path / "tool-runtime.db"
    monkeypatch.setenv("AGENTIC_TOOL_BUDGET_STORE", "sqlite")
    monkeypatch.setenv("AGENTIC_TOOL_BUDGET_PATH", str(path))
    tool = ToolSpec(
        name="expensive.tool",
        description="expensive",
        execute=lambda data: data,
        input_schema={},
        side_effect="read",
        cost_units=1,
    )
    context = ToolCallContext(run_id="run_1", step=1, action=Action.tool("expensive.tool", {}), tool=tool)
    pipeline = build_middleware_pipeline_from_env()
    governance = pipeline.middlewares[0]
    assert isinstance(governance, ToolGovernanceMiddleware)
    governance.policy.max_cost_units_per_run = 3

    observation = pipeline.execute_tool(context, lambda: {"ok": True})

    assert observation.ok is True
    assert observation.metadata["budgetStore"] == "SQLiteToolBudgetStore"
    assert path.exists()


def test_governance_approval_allows_high_risk_tool_when_context_is_approved() -> None:
    context = ToolCallContext(
        run_id="run_1",
        step=1,
        action=Action.tool("danger.delete", {}),
        tool=ToolSpec(
            name="danger.delete",
            description="danger",
            execute=lambda data: data,
            input_schema={},
            side_effect="write",
            risk_level="high",
        ),
        metadata={"approved": True},
    )

    observation = ToolGovernanceMiddleware().before_tool(context)

    assert observation is None
    assert context.metadata.get("approvalRequired") is None


class DangerPlanner:
    def next(self, context: object) -> Action:
        if not getattr(context, "trace"):
            return Action.tool("danger.delete", {}, reason="test", source="test")
        return Action.final("done", reason="test", source="test")


class FailingTraceSink:
    def record(self, span: object) -> None:
        raise RuntimeError("trace backend unavailable")


def _capture_urlopen(monkeypatch) -> list[dict]:
    sent: list[dict] = []

    class Response:
        status = 200

        def __enter__(self) -> "Response":
            return self

        def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
            return None

    def fake_urlopen(request: object, timeout: float) -> Response:
        data = getattr(request, "data")
        sent.append(
            {
                "url": getattr(request, "full_url"),
                "timeout": timeout,
                "headers": dict(getattr(request, "headers")),
                "payload": json.loads(data.decode("utf-8")),
            }
        )
        return Response()

    monkeypatch.setattr("agentic_core.tools.middleware.urlopen", fake_urlopen)
    return sent


def _otlp_attrs_to_dict(attributes: list[dict]) -> dict[str, dict]:
    return {str(item["key"]): item["value"] for item in attributes}
