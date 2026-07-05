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
pytest: 330 passed
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

- 已有学习版 `RuntimeIdentity` 身份上下文、本地 signed claims token 和 tenant policy JSON,但没有真实登录/OIDC/JWT provider/集中式租户策略服务。
- 事件日志已有本地 SQLite 后端,但还没有服务端数据库级事件平台和记忆后端。
- 没有外部 moderation、协作式人审平台和策略中心。
- 已有本地 hashing embedding 检索边界,但没有真实 embedding 模型和向量数据库。
- 已有本地 replay inspection bundle、人工标注数据流和带可选 Bearer Token / review 写入 API / review decisions 分页 API / SQLite review store / JSONL 审计事件的 eval governance server,但没有线上协作标注平台。
- 已有本地 HTML/JSON eval governance dashboard、标准库服务端认证、signed claims token、tenant policy JSON、写入/RBAC/review state 边界,但没有真实身份系统、集中式租户策略服务和多人协作的生产治理后台。

## 阶段 1: Typed State

状态: **完成学习版,生产形态良好。**

证据:

- `agentic_core/runtime/schemas.py`
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
- `agentic_core/event_payloads.py`
  - typed event payload dataclass
  - event payload required-field schema registry
  - payload validation result
  - payload schema migration
- `agentic_core/runtime/agent.py`
  - `Agent.run_typed() -> AgentRunResult`
  - `Agent.run() -> dict` 兼容旧 CLI/Chat
- `tests/test_typed_state.py`
- `docs/typed-state-refactor-plan.md`
- `docs/architecture.md`

已满足:

- 内部主链路不再依赖裸 dict。
- 对外 JSON 保持兼容字段,例如 `runId`、`memoryDecision`、`longTermMemories`。
- Safety/Memory/Trace/Event/Result 都有 typed 外壳。
- Agent 主链路事件 payload 使用 typed dataclass。
- `MemoryStore.record_event()` 会先迁移旧 payload,再写入 `payloadSchema.valid/errors/migrationsApplied` 校验结果。

仍未等同完整生产:

- 未引入 Pydantic / attrs / msgspec 等运行时 schema 校验库。
- Event payload 已有学习版 required-field schema 和 migration 层,但还不是强运行时 schema。

建议后续:

- 再考虑从 dataclass + required-field 校验迁移到 Pydantic v2 或 msgspec。

## 阶段 2: SafetyPolicy

状态: **完成生产化骨架。**

证据:

- `agentic_core/policies/safety.py`
  - `SafetyRule`
  - `RuleBasedSafetyPolicy`
  - `LlmSafetyPolicy`
  - `CompositeSafetyPolicy`
  - `build_safety_policy_from_env`
- `agentic_core/policies/safety_review.py`
  - `SafetyReviewQueue`
  - `InMemorySafetyReviewQueue`
  - `JsonlSafetyReviewQueue`
  - `build_safety_review_queue_from_env`
- `agentic_core/runtime/agent.py`
  - SafetyPolicy 在 MemoryPolicy / Planner / Tool 之前运行
  - action=review 时写入 SafetyReviewQueue 并记录 `safety_review_queued`
- `tests/test_safety_policy.py`
- `tests/test_contracts.py`
- `docs/architecture.md`
- `docs/operations.md`

已满足:

- 支持 `allow / warn / review / refuse` 分级动作。
- `review/refuse` 会阻断整轮。
- LLM checker 失败会回退规则版。
- Composite checker 可选 fail-open / fail-closed。
- review 动作可进入本地 SafetyReviewQueue。
- 支持 JSONL safety review queue,便于本地持久化审核证据。
- SafetyDecision 会进入事件日志和最终结果。

仍未等同完整生产:

- 没有接 OpenAI moderation 或其他外部安全服务。
- SafetyReviewQueue 仍是本地内存/JSONL 版本,不是协作式人审平台。
- 没有租户级安全策略中心。
- tool-output safety 已有 middleware 基础版,但还不是完整 DLP/外部内容安全服务。

建议后续:

- 增加外部 moderation adapter。
- 将 SafetyReviewQueue 接入协作式审核平台或工单系统。
- 将 request safety 与 tool safety 接入统一策略中心和外部内容安全服务。

## 阶段 3: Tool Metadata

状态: **完成学习版生产元数据。**

证据:

- `agentic_core/tools/registry.py`
  - `ToolSpec`
  - `ToolRegistry`
  - `to_public_dict()`
  - `input_schema_to_json_schema()`
  - `catalog()`
  - `validate_catalog()`
- `tests/test_tool_metadata.py`
- `tests/test_tool_schema_single_source.py`
- `docs/architecture.md`

已满足:

- 工具参数 schema 是单一真相源。
- 暴露 `inputJsonSchema` JSON Schema 子集,供外部治理和文档系统使用。
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
  - `owner`
  - `slaTier`
  - `dataClassification`
  - `auditClassification`
  - `externalSideEffect`
  - `lifecycleStatus`
  - `introducedIn`
  - `deprecatedIn`
  - `replacedBy`
  - `migrationNotes`
- Tool catalog 可保留 removed 工具供审计。
- deprecated/removed 工具必须声明替代工具和迁移说明。
- removed 工具不会暴露给 planner,直接执行也会失败。
- Planner prompt 和参数校验都从工具 registry 派生。

仍未等同完整生产:

- JSON Schema 仍是标准库子集,不是完整 OpenAPI/JSON Schema 工具目录。
- owner/SLA/audit classification 仍是本地静态元数据,未接服务目录或告警系统。
- 工具 lifecycle/migration 仍是本地校验,未接集中式发布流程。

建议后续:

- 将工具目录接入服务 owner / SLA / 数据分级系统。
- 接入集中式工具发布和版本迁移流程。

## 阶段 4: Middleware Pipeline

状态: **完成生产化学习版。**

证据:

- `agentic_core/tools/middleware.py`
  - `MiddlewarePipeline`
  - `RuntimeIdentity` 接入 ToolCallContext
  - `ToolGovernancePolicy`
  - `ToolGovernanceMiddleware`
  - `ToolBudgetStore`
  - `InMemoryToolBudgetStore`
  - `JsonFileToolBudgetStore`
  - `CostAccountingMiddleware`
  - `ToolOutputSafetyMiddleware`
  - `IdempotencyStore`
  - `InMemoryIdempotencyStore`
  - `JsonFileIdempotencyStore`
  - timeout / retry / idempotency / tool-output safety / OTel-style tool span sink
- `agentic_core/runtime/agent.py`
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
- 支持可注入 ToolBudgetStore;JSON 文件和 SQLite 后端可在同机进程间共享预算。
- 支持 timeout、retry、idempotency key。
- 支持 write 工具成功结果的进程内幂等存储;命中时短路返回第一次结果。
- 支持 JSON 文件和 SQLite 幂等后端,可在同机进程间共享 write 工具幂等结果。
- idempotency key 包含 run、step、tool、input、tool version、user 和 tenant。
- read 工具不缓存,失败 write 不缓存。
- 支持工具输出/错误敏感信息净化,复用 `SENSITIVE_PATTERN`。
- 支持可注入 `ToolTraceSink`;默认内存 span,也可写入 JSONL 或发送到 OTLP/HTTP collector。
- 工具 span 覆盖成功、失败、审批短路和幂等命中路径。
- span 只保存治理元数据和状态,不保存工具输入/输出。
- Observation metadata 记录审计字段。

仍未等同完整生产:

- timeout 用线程池学习版实现,不能强杀线程。
- budget 默认仍是内存版;JSON/SQLite 后端只适合同机学习或单机部署,不是 Redis/Postgres 级多机共享预算。
- 已有标准库 OTel-style span sink 和 OTLP/HTTP exporter,但还没有接官方 OpenTelemetry SDK、采样、跨组件上下文传播和自动 instrumentation。
- 幂等存储已有本地 JSON/SQLite 后端,但不是 Redis/Postgres 等多机持久后端。
- tool-output safety 仍是规则净化,不是完整 DLP/外部内容安全服务。
- RuntimeIdentity 由环境变量构造,不是登录态/JWT。

建议后续:

- 工具自身接底层 HTTP/DB timeout。
- 接入官方 OpenTelemetry SDK、采样和上下文传播。
- 用 Redis/Postgres budget store 管理多机预算。
- 用 Redis/Postgres idempotency store 管理跨进程写入类工具。

## 阶段 5: Persistent Event Log

状态: **完成本地 JSONL + SQLite 学习后端。**

证据:

- `agentic_core/observability/event_writer.py`
  - `EventWriter`
  - `MemoryEventWriter`
  - `JsonlEventWriter`
  - `SQLiteEventWriter`
  - `CompositeEventWriter`
  - redaction
  - rotation / retention / file lock
- `agentic_core/observability/event_log.py`
  - JSONL reader
  - SQLite reader
  - rotated backups reader
  - timeline formatter
- `agentic_core/memory/store.py`
  - `record_event()`
- `agentic_core/observability/event_payloads.py`
  - payload schema registry
  - typed payload dataclass
  - payload validation
- `tests/test_event_writer.py`
- `tests/test_event_log.py`
- `tests/test_event_payloads.py`
- `docs/persistent-event-log-production-plan.md`

已满足:

- EventWriter 抽象先于 JSONL 后端。
- 事件写入失败不影响主流程。
- 事件写入前脱敏。
- JSONL 支持大小轮转、备份保留、文件锁。
- SQLite 支持本地结构化查询,按 `(run_id, id)` 避免跨 run 事件 id 冲突。
- reader 默认读取轮转备份。
- run 生命周期事件基本完整。
- 核心事件 payload 写入前有 schema migration 和 `payloadSchema` 校验证据。

仍未等同完整生产:

- JSONL/SQLite 仍是本地后端,不是 Postgres/ClickHouse/OTel 这类集中式事件平台。
- 多机并发不适用。
- Event payload schema/migration 仍是标准库版本,不是 Pydantic/msgspec 强校验。
- 还没有 deterministic replay,只有 timeline inspection。

建议后续:

- 增加 Postgres/ClickHouse/OTel writer。
- 增加 EventRecord 外壳 schema migration 和更完整的 payload migration registry。
- 将 replay 明确拆成 timeline reconstruction 与 deterministic replay。

## 阶段 6: Memory Lifecycle

状态: **完成规则版生命周期治理。**

证据:

- `agentic_core/memory/store.py`
  - exact dedupe
  - semantic merge rules
  - importance
  - expiresAt
  - archive expired
  - prune by retention
  - JSON persistence
- `agentic_core/memory/lifecycle.py`
  - single lifecycle policy source
  - JSON policy load
  - policy inspect/validate CLI
  - semantic key
  - conflict key
  - importance
  - default expiry
  - retention sort key
- `agentic_core/memory/admin.py`
  - namespace list
  - archive
  - set-importance
  - conflicts
  - resolve-conflict
- `agentic_core/runtime/schemas.py`
  - MemoryRecord lifecycle fields
- `tests/test_memory_lifecycle.py`
- `tests/test_memory_lifecycle_policy.py`
- `tests/test_json_memory_store.py`
- `docs/architecture.md`

已满足:

- active/archived 状态。
- MemoryStore 和 memory_admin 共用 `MemoryLifecyclePolicy`,避免去重/冲突/importance/过期规则漂移。
- `AGENTIC_MEMORY_LIFECYCLE_POLICY_PATH` 可从 JSON 增量覆盖 TTL、positive score keys 和 type importance boosts。
- `python3 -m agentic_core.memory.lifecycle show/validate` 可查看和校验策略文件。
- 长期记忆带 userId/tenantId namespace,Agent 保存和读取 snapshot 时按当前 RuntimeIdentity 隔离。
- memory_admin 可按 namespace 查看、归档、调整 importance、查看 active 冲突组并解决冲突。
- memory review HTTP API 可按 namespace 查看、归档、调整 importance、查看/解决冲突。
- review 写接口要求 Bearer token,避免无认证修改长期记忆。
- 访问统计。
- 技术栈和学习时长偏好的规则语义合并。
- 重要性评分。
- task_state/task_context 默认过期。
- retention 归档而非删除。
- 本地 `HashingMemoryEmbeddingIndex` 支持 query -> long-term memory 相似度排序。
- Agent 给 planner/response 的 snapshot 会用当前 goal 做长期记忆相关性排序。
- JSON 持久化兼容旧文件。

仍未等同完整生产:

- 已有本地 hashing embedding 检索边界,但没有接真实 embedding 模型和向量数据库。
- 已有最小 memory review API,但没有图形化协作审核 UI。
- 语义合并和冲突检测仍是规则版,尚未接入真实 embedding/人工审核工作流。
- 生命周期策略已支持本地 JSON 外部化,尚未接入集中式租户级策略服务。

建议后续:

- 增加真实 embedding + 向量数据库后端。
- 在 memory review API 之上增加图形化协作审核 UI。

## 阶段 7: Evals

状态: **完成确定性质量门禁基线 + judge 骨架 + judge registry/version 治理 + 本地人工 label 校准 + 复核队列采样 + 多人一致性统计。**

证据:

- `evalops/harness.py`
  - `EvalCase`
  - `EvalCaseResult`
  - `EvalReport`
  - `EvalThresholds`
  - `collect_run_metrics`
- `evalops/dataset.py`
  - event-log-to-eval dataset draft
  - JSONL/SQLite event reader integration
  - reviewRequired dataset schema
- `evalops/replay.py`
  - replay inspection bundle
  - JSONL/SQLite event reader integration
  - timeline / tool calls / event counts extraction
- `evalops/governance/dashboard.py`
  - HTML/JSON governance dashboard
  - report/history/dataset aggregation
  - review queue/agreement/rubric validation summary
- `evalops/governance/server.py`
  - 标准库 governance server
  - 可选 Bearer Token 认证
  - signed claims token 验证
  - tenant policy JSON 授权
  - admin/viewer/reviewer scope RBAC
  - `/api/reviews/status` 多用户审核状态
  - 受保护的 `POST /api/reviews/apply`
  - review 写入路径由服务端配置,请求体不能指定路径
  - `eval_review_apply` / `eval_review_apply_failed` 审计事件
  - `/health`
  - `/dashboard`
  - `/api/dashboard`
  - `/api/rubrics`
- `evalops/review.py`
  - dataset review list/apply
  - approve/reject case decisions
  - judge 人工 label 写入
  - 多人复核 agreement 统计
  - `--require-reviewed` integration through eval_harness
- `evalops/sampling.py`
  - review queue 生成
  - priority/reason 采样
  - `agentic_eval_review_queue` JSON 输出
- `evalops/diff.py`
  - eval report JSON diff
  - metric/case/gate regression detection
  - `--fail-on-regression`
- `evalops/history.py`
  - append-only eval history JSONL
  - trend summary
  - latest-vs-previous regression hints
- `evalops/judge.py`
  - `EvalJudgeInput`
  - `JudgeDecision`
  - `RuleBasedEvalJudge`
  - `LlmEvalJudge`
- `evalops/judge_registry.py`
  - judge rubric registry
  - dataset rubric validation
  - CLI list/validate
- `tests/test_eval_harness.py`
- `tests/test_eval_dataset.py`
- `tests/test_eval_replay.py`
- `tests/test_eval_dashboard.py`
- `tests/test_eval_server.py`
- `tests/test_eval_review.py`
- `tests/test_eval_diff.py`
- `tests/test_eval_history.py`
- `tests/test_eval_judge.py`
- `tests/test_eval_judge_registry.py`
- `tests/test_eval_sampling.py`
- `docs/evals.md`

已满足:

- 默认 8 个确定性用例。
- 覆盖计算+笔记、长期记忆、学习计划、安全拒绝、敏感拒绝、技术栈追问、技术栈保存、计算失败。
- 支持 JSON 报告。
- 支持 gate failures。
- 可统计 event counts、tool success rate、planner fallback、memory saved、run failed。
- 可从 JSONL/SQLite event log 生成待审核 eval dataset 草稿。
- 可按 runId 生成 replay inspection bundle,用于本地复盘和人工复核。
- 可生成本地 HTML/JSON eval governance dashboard。
- 可启动标准库 eval governance server,暴露 health、HTML dashboard、JSON dashboard 和 judge rubric registry。
- governance server 支持 admin/viewer/reviewer Bearer Token;开启后除 `/health` 外都需要 `Authorization: Bearer ...`。
- governance server 支持本地 signed claims token,通过 HMAC 验证 `sub/tenant/scopes/exp`,scope 从 token claims 读取。
- governance server 支持 tenant policy JSON,启用后 tenant 必须存在、启用,并允许当前请求所需 scope。
- `eval.viewer` 允许读取 dashboard/API;`eval.reviewer` 允许 `POST /api/reviews/apply`。
- governance server 支持 `GET /api/reviews/status`,输出每个 case 的 reviewer、review session、currentStatus、conflicts 和 latestDecision。
- governance server 支持 `GET /api/reviews/decisions`,可按 case/reviewer/session 分页查询 SQLite review decisions。
- governance server 支持 `POST /api/reviews/apply`,复用 `eval_review.review_dataset()` 写出 golden dataset。
- 写入 API 必须配置 token、`--dataset` 和 `--review-output`,并拒绝客户端传文件路径。
- 写入 API 可通过 `--review-store` 或 `AGENTIC_EVAL_SERVER_REVIEW_STORE` 把新增 review decisions 写入 SQLite;状态查询启用该配置后从 SQLite 汇总多人审核状态。
- 写入 API 可通过 `--audit-events` 或 `AGENTIC_EVAL_SERVER_AUDIT_EVENTS` 输出 JSONL 审计事件。
- 审计事件写入失败不会阻断 review apply 主流程。
- governance server 对未支持的非 GET 路由返回 `405` 和 `Allow: GET`。
- 可从 dataset 生成按优先级排序的本地复核队列。
- 可批准/拒绝 dataset case,输出带 reviewer/notes 的 golden dataset。
- 可基于 `reviewDecisions` 统计 status/judge label 冲突和 conflict rate。
- `eval_harness --cases` 可加载 dataset 回归。
- `eval_harness --require-reviewed` 可阻止未审核草稿进入回归。
- `eval_diff` 可对比两次 JSON 报告,识别 gate/metric/case 回归。
- `eval_history` 可把 eval report 追加写入 JSONL 历史,并输出趋势摘要。
- `eval_harness --judge rule` 可启用离线确定性回答质量裁判。
- `eval_harness --judge llm` 可启用 Ollama LLM-as-judge,模型异常时回退 rule judge。
- `eval_judge_registry` 可登记并校验 `judgeRubric` / `judgeRubricVersion`。
- eval 启用 judge 时会校验当前 judge rubric 是否匹配 case 期望。
- eval report 汇总 `judge_evaluated`、`judge_passed`、`judge_pass_rate`,便于趋势监控。
- dataset case 可携带 `judgeRubric`、`expectedJudgeScore`、`expectedJudgePassed`、`judgeScoreTolerance`、`judgeNotes`。
- 启用 judge 后会检查人工 label mismatch 和 score drift,形成本地校准闭环。

仍未等同完整生产:

- 已有本地 replay inspection bundle、静态 governance dashboard 和带可选认证/signed claims token/tenant policy JSON/SQLite review store/写入审计边界的 governance server,但没有真实线上回放平台。
- 本地审核、复核队列、多人一致性统计和 judge label 已有,但没有协作式人工标注平台。
- 已有 LLM-as-judge 接口骨架、本地 label 校准、本地 registry/version 校验、静态 dashboard、本地 scope RBAC、signed claims token、tenant policy JSON、review state 和最小受保护写入 API,但没有真实身份系统、集中式租户策略服务和跨团队标注一致性看板。
- eval history 仍是本地 JSONL,还没有服务端趋势存储和可视化平台。
- event-log-to-eval 仍是本地草稿生成,还没有采样策略和多人审批工作流。

建议后续:

- 增加协作式 golden dataset 标注平台。
- 增加 eval 报告可视化和服务端趋势存储。

## 七阶段完成度

| 阶段 | 当前状态 | 证据强度 | 完整生产缺口 |
| --- | --- | --- | --- |
| Typed State | 已完成学习版 + payload schema + payload migration | 强 | Pydantic/msgspec 强运行时校验 |
| SafetyPolicy | 生产化骨架 + 本地 SafetyReviewQueue | 强 | 外部 moderation / 协作式人审平台 / 租户策略中心 |
| Tool Metadata | 已接入治理 + JSON Schema 子集导出 + owner/SLA/classification + lifecycle migration 校验 | 强 | OpenAPI 级目录 / 服务目录集成 |
| Middleware Pipeline | 生产化学习版 + JSON/SQLite budget/idempotency store + OTel-style tool span sink + OTLP/HTTP exporter | 强 | 官方 OTel SDK/采样/上下文传播 / Redis/Postgres budget 与幂等 |
| Persistent Event Log | 本地 JSONL + SQLite + payloadSchema + payload migration | 强 | Postgres / ClickHouse / OTel / deterministic replay |
| Memory Lifecycle | `MemoryLifecyclePolicy` 单一策略源 + JSON 策略配置 + user/tenant namespace + memory_admin CLI/API + conflict resolution + 本地 embedding search | 强 | 真实 embedding/向量库 / 协作审核 UI / 集中式租户级策略服务 |
| Evals | 确定性 gate + dashboard + 带本地 RBAC、signed claims token、tenant policy JSON、review state、review decisions 分页 API、review 写入 API、SQLite review store 和审计事件的 governance server + replay bundle + dataset review + review queue + agreement + judge registry + judge label 校准 + report diff + history + judge 骨架 | 强 | 线上回放平台 / 协作标注平台 / OIDC/JWT provider 与集中式策略中心 |

## 结论

当前项目已达到“可学习、可演示、可回归、可继续演进”的阶段性 100%。

但如果把目标定义为“真实公司生产环境可直接上线”,还需要继续补:

1. 真实用户/租户认证授权系统(OIDC/JWT provider/session/集中式策略中心;当前只到本地静态 token + signed claims token + tenant policy JSON + scope RBAC)。
2. 数据库后端(Postgres/SQLite/ClickHouse/OTel)。
3. 外部 moderation + 协作式 human review。
4. 真实 embedding memory 后端和向量数据库。
5. 官方 OTel SDK、采样和跨组件上下文传播。
6. 线上数据回放 eval。

因此本审计不建议把总目标标记为“最终完成”。建议继续按上述生产缺口推进下一阶段。
