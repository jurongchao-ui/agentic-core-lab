# Typed State 改造实施计划

## Summary

本阶段目标是把 Agent 内部主链路从裸 `dict[str, Any]` 升级为稳定 typed state，同时保持 CLI/Chat 的旧 JSON 输出兼容。

改造范围只包含 `agentic` Python 主线，不修改 `weather-mcp`，不写 Obsidian，不删除 JS v0 参考代码。

## Typed State 边界

内部主链路尽量使用 dataclass / Literal / Protocol：

```text
goal
  -> SafetyDecision
  -> MemoryDecision
  -> PlannerContext
  -> Action
  -> Observation
  -> TraceStep
  -> ResponseContext
  -> ResponseDecision
  -> AgentRunResult
```

允许继续使用 `dict[str, Any]` 的边界：

- CLI / Chat 最终 JSON 输出
- Ollama API 请求/响应
- Tool input/output
- `to_dict()` 兼容层

## 新增核心类型

在 `agentic_core/schemas.py` 中新增：

- `RunStatus`
- `EventType`
- `MemoryType`
- `NoteRecord`
- `TodoRecord`
- `MemoryRecord`
- `EventRecord`
- `MemorySnapshot`
- `TraceStep`
- `AgentRunState`
- `AgentRunResult`

内部字段使用 snake_case；对外 JSON 继续保持旧字段名，例如 `runId`、`memoryDecision`、`longTermMemories`。

## Agent 主链路改造

- 新增 `Agent.run_typed(goal: str) -> AgentRunResult` 作为真实主逻辑。
- 保留 `Agent.run(goal: str) -> dict[str, Any]` 作为兼容包装。
- Agent 内部只维护 `AgentRunState`。
- trace 使用 `list[TraceStep]`。
- saved memories 使用 `list[MemoryRecord]`。
- 每种提前返回都设置明确状态：
  - `refused`
  - `clarification`
  - `completed`
  - `incomplete`
  - `failed`

global safety 拦截时不再伪造普通 MemoryPolicy 执行结果；兼容 JSON 中的 `memoryDecision` 使用 `metadata.source = "skipped_by_safety"` 标记跳过原因。

## MemoryStore / Planner / ResponsePolicy / TraceView

- `MemoryStore` 内部从 dict list 改为 typed record list。
- `snapshot()` 返回 `MemorySnapshot`。
- `PlannerContext` 从 `TypedDict` 改成 dataclass。
- Planner 只接收 `MemorySnapshot`，不再接收完整 `MemoryStore`。
- Planner helper 函数接受 `list[TraceStep]`。
- ResponsePolicy 的 `ResponseContext` 使用 typed fields。
- ResponsePolicy 通过 `step.action` / `step.observation` 读取 trace。
- CLI/Chat 继续调用 `Agent.run()`，Trace JSON 输出保持旧结构。

## Test Plan

新增 `tests/test_typed_state.py`：

- `TraceStep.to_dict()` 输出旧 trace JSON。
- `MemorySnapshot.to_dict()` 输出 `longTermMemories` / `recentEvents`。
- `AgentRunResult.to_dict()` 输出兼容旧 CLI 的结构。
- `MemoryStore.snapshot()` 返回 `MemorySnapshot`。
- `Agent.run_typed()` 返回 `AgentRunResult`。
- `Agent.run()` 仍返回 dict。

回归检查：

```bash
cd /Users/jurongchao/Desktop/ai学习测试库/agentic
python3 -m compileall agentic_core examples tests
.venv/bin/python -m pytest -q
.venv/bin/python -m mypy agentic_core
```

重点验收：

- 计算并记录笔记仍成功。
- 技术栈长期记忆仍保存。
- 缺失记忆内容时仍追问。
- global safety 拦截时 trace 为空，MemoryPolicy 被跳过。
- 计算失败时不写依赖笔记。

## 验收标准

- `Agent.run_typed()` 是 typed 主入口。
- `Agent.run()` 输出旧 JSON 结构。
- `MemoryStore` 内部不再保存裸 dict。
- Planner 和 ResponsePolicy 不再依赖 dict trace。
- 旧 CLI/Chat 使用方式不变。
