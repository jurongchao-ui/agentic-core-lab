# Agentic Core Architecture

## 主链路

```text
Agent.run(goal)
  -> SafetyPolicy.check
  -> MemoryPolicy.evaluate
  -> MemoryStore.add_long_term_memory
  -> Plan-Act-Observe loop
       Planner.next
       MiddlewarePipeline.execute_tool
       ToolRegistry.execute
       Observation
  -> ResponsePolicy.decide
  -> AgentRunResult
```

## 模块职责

```text
agentic_core/
  cli.py              # 单次运行入口
  chat.py             # 连续对话入口
  agent.py            # 主编排,Plan-Act-Observe loop
  contracts.py        # Protocol 契约 + PlannerContext
  schemas.py          # Typed State: Action/Observation/AgentRunResult 等
  planner.py          # HermesPlanner + RuleBasedPlanner
  memory_policy.py    # RuleBasedMemoryPolicy + LlmMemoryPolicy
  memory.py           # MemoryStore / JsonMemoryStore
  memory_lifecycle.py # 长期记忆去重/冲突/importance/过期/retention 策略
  memory_admin.py     # 长期记忆审核/归档/调权重/冲突解决 CLI
  safety_policy.py    # RuleBased/Llm/Composite SafetyPolicy
  response_policy.py  # 最终回复仲裁
  middleware.py       # 工具执行横切管道
  tools.py            # ToolRegistry + ToolSpec + 内置工具
  event_payloads.py   # EventRecord.payload 的事件类型级 schema
  event_writer.py     # EventWriter 抽象 + JSONL/SQLite writer
  event_log.py        # JSONL/SQLite 事件读取和时间线查看
  eval_harness.py     # 确定性 eval + gate
  trace_view.py       # 可读 trace 渲染
```

## Typed State

内部主链路使用 typed dataclass,对外 JSON 保持兼容字段名:

- `Action`: planner 输出的工具动作或 final 动作。
- `Observation`: 工具执行结果,含 metadata 审计字段。
- `MemoryDecision`: 是否保存长期记忆。
- `SafetyDecision`: 请求级安全判断。
- `TraceStep`: 一次 Plan-Act-Observe step。
- `MemorySnapshot`: Planner/Responder 可读的记忆快照。
- `AgentRunResult`: 单次 run 聚合结果。

原则:

- 内部字段使用 snake_case。
- 对外 JSON 使用既有字段,例如 `runId`、`memoryDecision`、`longTermMemories`。
- `Agent.run_typed()` 是主入口,`Agent.run()` 只做兼容包装。

## Memory

记忆分层:

- `notes`: 学习笔记。
- `todos`: 待办。
- `events`: 当前进程事件列表。
- `long_term_memories`: 长期记忆。

`MemoryPolicy` 判断一句话是否值得保存:

- 规则版按维度评分。
- LLM 版做语义抽取,程序做敏感一票否决、置信度阈值和类型校验。

长期记忆生命周期:

- `MemoryLifecyclePolicy`: 长期记忆治理规则的单一真相源。
- `status`: active / archived。
- `userId` / `tenantId`: 长期记忆所属用户和租户,Agent snapshot 只读取当前 identity namespace。
- `importance`: 0-100。
- `expiresAt`: 阶段性任务状态默认过期。
- `mergedFrom`: 规则语义合并保留历史文本。
- `accessCount` / `lastAccessedAt`: snapshot 给 planner 时更新。

当前语义合并和冲突检测是规则版,覆盖技术栈和学习时长偏好。`MemoryStore` 和 `memory_admin` 共用同一个 `MemoryLifecyclePolicy`,并可通过 `AGENTIC_MEMORY_LIFECYCLE_POLICY_PATH` 从 JSON 增量覆盖 TTL、positive score keys 和 type importance boosts。`memory/embedding.py` 提供本地 `HashingMemoryEmbeddingIndex`,让 snapshot 可按当前 goal 做相似度排序;生产可继续替换为真实 embedding/向量库、数据库唯一键或租户级策略中心。

`memory_admin.py` 提供本地审核维护入口:

- 按 `userId` / `tenantId` 查看长期记忆。
- 归档长期记忆,不物理删除。
- 手动调整 `importance`,影响 retention 排序。
- 查看 active 长期记忆冲突组,例如技术栈、学习时长偏好的多版本冲突。
- 解决冲突时保留一条记忆,软归档同组其他记忆。

`memory/server.py` 在同一套治理函数上提供标准库 HTTP API:

- `GET /api/memories`: 按 namespace 查看长期记忆。
- `GET /api/memories/conflicts`: 查看冲突组。
- `POST /api/memories/archive`: 归档记忆。
- `POST /api/memories/importance`: 调整 importance。
- `POST /api/memories/resolve-conflict`: 解决冲突。

写接口必须配置 Bearer token,避免本地服务误暴露后被无认证修改记忆。

## Safety

`SafetyPolicy` 在最前面运行。命中 global safety 时:

- 跳过 MemoryPolicy。
- 不进入 Planner。
- 不执行工具。
- ResponsePolicy 返回 global_safety 档。

实现:

- `RuleBasedSafetyPolicy`: 确定性规则。
- `LlmSafetyPolicy`: LLM/moderation JSON 边界,失败回退规则版。
- `CompositeSafetyPolicy`: 多 checker 并联,选择最高风险结果。

动作:

- `allow`
- `warn`
- `review`
- `refuse`

当前 `review/refuse` 会阻断整轮。
当 action 为 `review` 时,Agent 会把请求写入 `SafetyReviewQueue`,
并记录 `safety_review_queued` 事件。默认队列是进程内存版;设置
`AGENTIC_SAFETY_REVIEW_QUEUE=jsonl` 可追加写入本地 JSONL。

## Tool Governance

`RuntimeIdentity` 表示当前 run 的身份上下文:

- `userId`
- `tenantId`
- `roles`
- `permissionScopes`

CLI/Chat 可从环境变量构造身份。AgentRunResult、run events、tool observation metadata 都会携带 identity,用于审计和权限判断。

`ToolSpec` 是工具治理元数据的单一真相源:

- `permissionScope`
- `sideEffect`
- `timeoutMs`
- `costUnits`
- `retryCount`
- `riskLevel`
- `requiresApproval`
- `guardSensitive`
- `version`
- `owner`
- `slaTier`
- `dataClassification`
- `auditClassification`
- `externalSideEffect`
- `inputJsonSchema`
- `lifecycleStatus`
- `introducedIn`
- `deprecatedIn`
- `replacedBy`
- `migrationNotes`

`inputSchema` 仍是 planner 参数校验的单一真相源;`inputJsonSchema` 是只读导出,
用于外部治理、文档和后续 schema 标准化。

`ToolRegistry.catalog()` 会导出完整工具目录,包含已 removed 的历史工具;
`ToolRegistry.validate_catalog()` 会检查 deprecated/removed 工具必须声明替代工具和迁移说明。
`ToolRegistry.list()` 不会把 removed 工具暴露给 planner。

工具执行统一经过 `MiddlewarePipeline.execute_tool()`:

```text
ToolGovernanceMiddleware
  -> CostAccountingMiddleware
  -> timeout/retry/write-tool idempotency/tool-output safety/tracing
```

`ToolGovernancePolicy` 支持:

- allowed permission scopes。若策略未显式配置,会使用 `RuntimeIdentity.permissionScopes`。
- denied permission scopes。
- 按 risk level 要求审批。
- 按 side effect 要求审批。
- 每个 tenant + run 的 cost budget。

`ToolGovernanceMiddleware` 带可注入 `ToolBudgetStore`。默认 `InMemoryToolBudgetStore`
保持轻量;设置 `AGENTIC_TOOL_BUDGET_STORE=json` 可使用 `JsonFileToolBudgetStore`,
让同一台机器上的 CLI/chat 进程共享 tenant+run 预算;设置
`AGENTIC_TOOL_BUDGET_STORE=sqlite` 可使用 `SQLiteToolBudgetStore`,用 SQLite 事务保护
预算 reserve 的读改写过程。

`MiddlewarePipeline` 带可注入 `IdempotencyStore`。默认 `InMemoryIdempotencyStore`
只缓存成功的 write 工具结果;设置 `AGENTIC_IDEMPOTENCY_STORE=json` 可使用
`JsonFileIdempotencyStore`,让同机 CLI/chat 进程共享 write 工具幂等结果;设置
`AGENTIC_IDEMPOTENCY_STORE=sqlite` 可使用 `SQLiteIdempotencyStore`,把已脱敏的
Observation JSON 保存进 SQLite。
read 工具不缓存,失败 write 不缓存,idempotency key 包含 run、step、tool、input、
tool version、user 和 tenant,避免不同身份误命中。

`ToolOutputSafetyMiddleware` 会在工具执行后递归净化 `Observation.output/error`,
复用同一份 `SENSITIVE_PATTERN`,防止敏感工具输出进入最终回复、trace 或事件。

`MiddlewarePipeline` 还带可注入 `ToolTraceSink`。默认 `InMemoryToolTraceSink`
只在进程内保留 span;设置 `AGENTIC_TOOL_TRACE_SINK=jsonl` 可使用
`JsonlToolTraceSink` 追加写入 `data/tool-spans.jsonl`;设置
`AGENTIC_TOOL_TRACE_SINK=otlp_http` 可使用 `OtlpHttpToolTraceSink` POST 到
OTLP/HTTP collector 的 `/v1/traces`。span 使用 OTel-style
字段(`traceId/spanId/parentSpanId/status/attributes`),覆盖成功、工具失败、审批短路
和幂等命中等路径。span 只保存治理元数据和状态,不保存工具输入/输出,避免形成第二份敏感数据副本。

每次工具结果都会把审计信息写入 `Observation.metadata`。

## ResponsePolicy

最终回复由 ResponsePolicy 仲裁,防止 responder 覆盖系统事实。

优先级:

```text
global_safety
clarification
local_safety
memory_confirmation
tool_result_summary
failure_incomplete
planner_answer
normal_responder
```

原则:

- 已保存的记忆必须能确认。
- 工具失败必须如实说明。
- 敏感信息不保存也不能回显原文。
- 闲聊可以交给 responder,任务事实不能交给 responder 随意改写。

## Event Log

`EventRecord` 是跨 run 的持久事件单位。当前有两个本地学习后端:

- JSONL: append-only 文件,适合 tail/grep/导出。
- SQLite: 本地结构化数据库,适合按 runId/type/createdAt 查询。

事件 payload 也有轻量 schema:

- `event_payloads.py` 定义每种事件的 required fields。
- Agent 主链路使用 typed payload dataclass 发事件,减少裸 dict。
- `MemoryStore.record_event()` 写入前先做 payload schema migration,再校验 payload,并在事件里写入 `payloadSchema`。
- 旧 dict 调用仍兼容;能识别的旧扁平格式会迁成当前结构,缺字段时不打断主流程,但 `payloadSchema.valid=false` 会留下审计证据。
- `JsonMemoryStore` 读取旧 v1 事件时也会迁移 payload,并在 `payloadSchema.migrationsApplied` 留下迁移证据。

JSONL 后端特性:

- append-only。
- 一行一个事件。
- 写入前脱敏。
- 写入失败不影响主流程。
- 支持大小轮转、备份保留和 `.lock` 文件。
- reader 默认读取轮转备份。

SQLite 后端特性:

- 使用标准库 `sqlite3`,不引入第三方依赖。
- `events` 表保存完整 `event_json` 和单独 `payload_json`。
- 主键是 `(run_id, id)`,避免不同进程的 `event_1` 互相覆盖。
- 提供 runId/type/createdAt 索引,方便本地排障查询。

核心事件:

```text
run_started
safety_decision / safety_refusal
memory_decision / memory_saved / memory_clarification
planner_action / planner_fallback / planner_skipped
tool_started / tool_observation
response_decision
run_completed / run_failed
```
