# Production Readiness Audit

## 审计范围

本审计按之前确定的 7 个生产化阶段检查当前 `agentic_core`:

1. Typed State
2. SafetyPolicy
3. Tool Metadata
4. Middleware Pipeline
5. Persistent Event Log
6. Memory Lifecycle
7. Evals

判断标准:

- 是否有明确 typed schema / protocol。
- 是否接入 Agent 主链路。
- 是否有确定性测试覆盖。
- 是否有文档说明。
- 是否通过当前门禁。
- 是否仍只是学习版,距离完整生产还差什么。

当前门禁状态:

```text
pytest: 152 passed
mypy: success
compileall: passed
eval harness: 8/8 passed, Gate PASS
```

## 总结

当前项目已经不是“最小 demo”,而是一个 **production-shaped learning runtime**:

- 核心边界已经类型化。
- 关键横切能力有统一挂载点。
- 事件、记忆、安全、工具和回复都有结构化证据。
- eval harness 已能做确定性回归门禁。

但它还不是完整生产系统:

- 已有学习版 `RuntimeIdentity` 身份上下文,但没有真实登录/JWT/租户策略中心。
- 事件日志已有本地 SQLite 后端,但还没有服务端数据库级事件平台和记忆后端。
- 没有外部 moderation、人审队列和策略中心。
- 没有 embedding/向量记忆。
- 没有线上 replay/人工标注 eval 数据集。

## 阶段 1: Typed State

状态: **完成学习版,生产形态良好。**

证据:

- `agentic_core/schemas.py`
  - `Action`
  - `Observation`
  - `MemoryDecision`
  - `SafetyDecision`
  - `MemoryRecord`
  - `EventRecord`
  - `MemorySnapshot`
  - `TraceStep`
  - `AgentRunState`
  - `AgentRunResult`
- `agentic_core/agent.py`
  - `Agent.run_typed() -> AgentRunResult`
  - `Agent.run() -> dict` 兼容旧 CLI/Chat
- `tests/test_typed_state.py`
- `docs/typed-state-refactor-plan.md`
- `docs/architecture.md`

已满足:

- 内部主链路不再依赖裸 dict。
- 对外 JSON 保持兼容字段,例如 `runId`、`memoryDecision`、`longTermMemories`。
- Safety/Memory/Trace/Event/Result 都有 typed 外壳。

仍未等同完整生产:

- 未引入 Pydantic / attrs / msgspec 等运行时 schema 校验库。
- Event payload 仍是 `dict[str, Any]`,没有按事件类型拆成独立 payload schema。
- 没有 schema migration 工具。

建议后续:

- 如果继续生产化,优先给 `EventRecord.payload` 做事件类型级 schema。
- 再考虑从 dataclass 迁移到 Pydantic v2 或 msgspec。

## 阶段 2: SafetyPolicy

状态: **完成生产化骨架。**

证据:

- `agentic_core/safety_policy.py`
  - `SafetyRule`
  - `RuleBasedSafetyPolicy`
  - `LlmSafetyPolicy`
  - `CompositeSafetyPolicy`
  - `build_safety_policy_from_env`
- `agentic_core/agent.py`
  - SafetyPolicy 在 MemoryPolicy / Planner / Tool 之前运行
- `tests/test_safety_policy.py`
- `tests/test_contracts.py`
- `docs/architecture.md`
- `docs/operations.md`

已满足:

- 支持 `allow / warn / review / refuse` 分级动作。
- `review/refuse` 会阻断整轮。
- LLM checker 失败会回退规则版。
- Composite checker 可选 fail-open / fail-closed。
- SafetyDecision 会进入事件日志和最终结果。

仍未等同完整生产:

- 没有接 OpenAI moderation 或其他外部安全服务。
- 没有人审队列。
- 没有租户级安全策略。
- 没有按工具调用内容做更细粒度的 tool-output safety。

建议后续:

- 增加外部 moderation adapter。
- 增加 human review queue 的接口。
- 将 request safety 与 tool safety 分层。

## 阶段 3: Tool Metadata

状态: **完成学习版生产元数据。**

证据:

- `agentic_core/tools.py`
  - `ToolSpec`
  - `ToolRegistry`
  - `to_public_dict()`
- `tests/test_tool_metadata.py`
- `tests/test_tool_schema_single_source.py`
- `docs/architecture.md`

已满足:

- 工具参数 schema 是单一真相源。
- 每个工具暴露:
  - `permissionScope`
  - `sideEffect`
  - `timeoutMs`
  - `costUnits`
  - `retryCount`
  - `riskLevel`
  - `requiresApproval`
  - `guardSensitive`
  - `version`
- Planner prompt 和参数校验都从工具 registry 派生。

仍未等同完整生产:

- 没有工具 schema 的 JSON Schema 标准化导出。
- 没有工具版本迁移策略。
- 没有工具 owner / SLA / audit classification。

建议后续:

- 将 `input_schema` 升级为 JSON Schema 子集。
- 给工具增加 owner、data classification、external side effect level。

## 阶段 4: Middleware Pipeline

状态: **完成生产化学习版。**

证据:

- `agentic_core/middleware.py`
  - `MiddlewarePipeline`
  - `RuntimeIdentity` 接入 ToolCallContext
  - `ToolGovernancePolicy`
  - `ToolGovernanceMiddleware`
  - `CostAccountingMiddleware`
  - timeout / retry / idempotency / tracing metadata
- `agentic_core/agent.py`
  - 工具执行统一走 `MiddlewarePipeline.execute_tool()`
- `tests/test_middleware.py`
- `docs/architecture.md`
- `docs/operations.md`

已满足:

- before/after/tool execution 统一入口。
- 支持 permission allow/deny。
- 支持从 RuntimeIdentity.permissionScopes 派生当前身份授权范围。
- 支持 risk/sideEffect 审批。
- 支持 tenant + run 级 cost budget。
- 支持 timeout、retry、idempotency key。
- Observation metadata 记录审计字段。

仍未等同完整生产:

- timeout 用线程池学习版实现,不能强杀线程。
- budget 只存在 middleware 实例内存里,没有跨进程共享。
- 没有 OpenTelemetry span。
- 没有真实幂等存储,只有 idempotency key 生成。
- RuntimeIdentity 由环境变量构造,不是登录态/JWT。

建议后续:

- 工具自身接底层 HTTP/DB timeout。
- 添加 OTel tracing adapter。
- 用持久 idempotency store 管理写入类工具。

## 阶段 5: Persistent Event Log

状态: **完成本地 JSONL + SQLite 学习后端。**

证据:

- `agentic_core/event_writer.py`
  - `EventWriter`
  - `MemoryEventWriter`
  - `JsonlEventWriter`
  - `SQLiteEventWriter`
  - `CompositeEventWriter`
  - redaction
  - rotation / retention / file lock
- `agentic_core/event_log.py`
  - JSONL reader
  - SQLite reader
  - rotated backups reader
  - timeline formatter
- `agentic_core/memory.py`
  - `record_event()`
- `tests/test_event_writer.py`
- `tests/test_event_log.py`
- `docs/persistent-event-log-production-plan.md`

已满足:

- EventWriter 抽象先于 JSONL 后端。
- 事件写入失败不影响主流程。
- 事件写入前脱敏。
- JSONL 支持大小轮转、备份保留、文件锁。
- SQLite 支持本地结构化查询,按 `(run_id, id)` 避免跨 run 事件 id 冲突。
- reader 默认读取轮转备份。
- run 生命周期事件基本完整。

仍未等同完整生产:

- JSONL/SQLite 仍是本地后端,不是 Postgres/ClickHouse/OTel 这类集中式事件平台。
- 多机并发不适用。
- Event payload 没有强 schema。
- 还没有 deterministic replay,只有 timeline inspection。

建议后续:

- 增加 Postgres/ClickHouse/OTel writer。
- 增加事件 schema version migration。
- 将 replay 明确拆成 timeline reconstruction 与 deterministic replay。

## 阶段 6: Memory Lifecycle

状态: **完成规则版生命周期治理。**

证据:

- `agentic_core/memory.py`
  - exact dedupe
  - semantic merge rules
  - importance
  - expiresAt
  - archive expired
  - prune by retention
  - JSON persistence
- `agentic_core/schemas.py`
  - MemoryRecord lifecycle fields
- `tests/test_memory_lifecycle.py`
- `tests/test_json_memory_store.py`
- `docs/architecture.md`

已满足:

- active/archived 状态。
- 访问统计。
- 技术栈和学习时长偏好的规则语义合并。
- 重要性评分。
- task_state/task_context 默认过期。
- retention 归档而非删除。
- JSON 持久化兼容旧文件。

仍未等同完整生产:

- 没有 embedding/向量检索。
- 没有 memory review UI。
- 没有 per-user/per-tenant namespace。
- 没有 memory conflict resolution 策略。

建议后续:

- 增加 namespace/user id 字段。
- 增加向量检索后端。
- 增加人工审核与 memory edit/delete 接口。

## 阶段 7: Evals

状态: **完成确定性质量门禁基线。**

证据:

- `agentic_core/eval_harness.py`
  - `EvalCase`
  - `EvalCaseResult`
  - `EvalReport`
  - `EvalThresholds`
  - `collect_run_metrics`
- `tests/test_eval_harness.py`
- `docs/evals.md`

已满足:

- 默认 8 个确定性用例。
- 覆盖计算+笔记、长期记忆、学习计划、安全拒绝、敏感拒绝、技术栈追问、技术栈保存、计算失败。
- 支持 JSON 报告。
- 支持 gate failures。
- 可统计 event counts、tool success rate、planner fallback、memory saved、run failed。

仍未等同完整生产:

- 没有真实线上数据回放。
- 没有人工标注集。
- 没有 LLM-as-judge。
- 没有跨版本报告对比。
- 没有按 event log 自动生成 eval cases。

建议后续:

- 增加 event-log-to-eval 工具。
- 增加 golden dataset 文件格式。
- 增加 JSON 报告 diff。

## 七阶段完成度

| 阶段 | 当前状态 | 证据强度 | 完整生产缺口 |
| --- | --- | --- | --- |
| Typed State | 已完成学习版 | 强 | payload schema / migration |
| SafetyPolicy | 生产化骨架 | 强 | 外部 moderation / 人审 |
| Tool Metadata | 已接入治理 | 强 | JSON Schema / owner / SLA |
| Middleware Pipeline | 生产化学习版 | 强 | OTel / 跨进程 budget / 持久幂等 |
| Persistent Event Log | 本地 JSONL + SQLite 后端 | 强 | Postgres / ClickHouse / OTel / deterministic replay |
| Memory Lifecycle | 规则版治理 | 强 | embedding / namespace / conflict resolution |
| Evals | 确定性 gate | 强 | 线上回放 / 标注集 / judge |

## 结论

当前项目已达到“可学习、可演示、可回归、可继续演进”的阶段性 100%。

但如果把目标定义为“真实公司生产环境可直接上线”,还需要继续补:

1. 真实用户/租户认证授权系统(JWT/session/策略中心)。
2. 数据库后端(Postgres/SQLite/ClickHouse/OTel)。
3. 外部 moderation + human review。
4. embedding memory 后端。
5. OTel tracing。
6. 线上数据回放 eval。

因此本审计不建议把总目标标记为“最终完成”。建议继续按上述生产缺口推进下一阶段。
