---
title: Agentic Core 加固开发日志
type: dev_log
date: 2026-07-02
scope: agentic_core
---

# Agentic Core 加固开发日志（2026-07-02）

本轮围绕一次代码评审展开，逐条修掉了发现的问题，并按"每步小改动 → 跑测试 → 看过程"的方式推进。全部改动零新依赖，风格与既有的"规则层 + LLM 层 + 程序把关"一致。

后续又补齐了 Typed State、Persistent Event Log、JSON 记忆持久化、学习计划工具、Eval Harness、Tool Metadata、Middleware Pipeline、Memory Lifecycle、结构化 SafetyPolicy、Event Payload Schema、Event Log Replay Bundle、Event Log to Eval Dataset、Eval Dataset Review、Eval Review Queue Sampling、Eval Review State、Eval Review Agreement、Eval Review Store、Eval Review Decisions API、Eval Judge Registry、Eval Governance Dashboard、Eval Governance Server、Eval Server Auth/RBAC、Signed Claims Token、Tenant Policy JSON、Eval Review Apply API、Eval Review Audit Events、Eval Report Diff、Eval History、Eval Judge、Judge Label Calibration、ToolTraceSink、OTLP/HTTP Tool Trace Exporter、SQLite Tool Runtime Stores、Local Memory Embedding Search、Memory Review API，以及 Ollama `format:"json"`。当前收尾状态：**pytest 330 passed / mypy success / eval harness 8 passed**。

---

## 改动清单（按完成顺序）

### 1. memory.add 网关化，堵住绕过 MemoryPolicy 的后门
- **问题**：`memory.add` 工具直接暴露给 LLM planner，模型可用任意 text + 自定义 scores 写长期记忆，绕过阈值和敏感检查。设计意图（"长期记忆由程序把关"）与实现矛盾。
- **修复**：[registry.py](../agentic_core/tools/registry.py) 把 `memory.add` 改成 `_memory_add` 方法，强制走 `MemoryPolicy.evaluate()`；模型只提议 text，是否保存/分类/评分全由 policy 决定。`ToolRegistry` 注入 `memory_policy`。
- **测试**：`tests/test_memory_add_gating.py`（含敏感信息拦截、忽略模型自评分）。

### 2. 工具参数 schema 单一真相源
- **问题**：新增工具要同时改三处（tools 注册、planner 的 `toolInputSchemas`、`validate_tool_input` 的 required），易漂移。
- **修复**：schema 挂到 `ToolRegistry` 注册处，`tools.list()` 携带 `inputSchema`；planner 的 prompt 提示和参数校验都从 `available_tools` 派生。纯重构，行为不变。
- **测试**：`tests/test_tool_schema_single_source.py`（含"注册即校验生效、不碰 planner"）。

### 3. MemoryPolicy 稳健化：LLM 抽取 + 规则兜底
- **问题**：用正则做语义判断，脆弱（"用 Python 算一下"误判成用户画像；"我是前端开发"又漏判）。
- **方案**：参考 mem0/Letta/LangMem 的共识（语义交给 LLM，程序把关），复用本项目 `HermesPlanner` 已有的"LLM 提议 → 程序校验 → 规则兜底"模式。
- **修复**：[memory_policy.py](agentic_core/memory_policy.py) 拆成基类 `MemoryPolicy` + `RuleBasedMemoryPolicy`（原逻辑，作 fallback）+ `LlmMemoryPolicy`（结构化抽取）。**关键控制点：敏感一票否决用程序侧正则做，不依赖模型**。敏感词提升为共享常量 `SENSITIVE_PATTERN`。装配层加 `AGENTIC_MEMORY_POLICY` 开关。抽出 [json_utils.py](agentic_core/json_utils.py) 共享 `extract_json_object`。
- **测试**：`tests/test_llm_memory_policy.py`（stub client，无需真实 Ollama）。

### 4. 修 confidence 解析崩溃（间歇性丢失记忆）
- **问题**：本地小模型把 `confidence` 返回成 null/非数字/0-1 小数时，`int()` 抛异常 → 静默回退规则版 → 规则版给"我是前端开发"打 5 分（<7）不保存。表现为间歇性没存。
- **修复**：新增 `coerce_confidence`——float 安全转换，无法解析用阈值默认值，0-1 量纲归一到 0-100，永不抛异常。
- **测试**：`test_malformed_confidence_does_not_crash`（None/"high"/0.9）。

### 5. 可观测性：捕获 LLM 原始输出 + 可读分步 trace
- **问题**：调试时"看不到过程"——LLM 原始输出从未被捕获，回退是静默的。
- **修复**：[schemas.py](../agentic_core/runtime/schemas.py) 给 `Action`/`MemoryDecision` 加 `metadata`；两个 LLM 边界（`HermesPlanner`、`LlmMemoryPolicy`）改成"先存 raw 再解析"，成功/回退都写 `source + rawModelOutput + error`。新增 [trace_view.py](../agentic_core/observability/trace_view.py) 渲染可读分步。统一开关 `AGENTIC_TRACE=off|brief|json`（chat 默认 brief，cli 默认 json）。
- **测试**：`tests/test_trace_view.py` + metadata 捕获断言。

### 6. 加自然语言回复能力（它原来不回话）
- **问题**：这套系统是"任务 agent"，对闲聊（"你好…"）只回空的任务报告模板，不会回话。
- **修复**：新增 [responder.py](agentic_core/responder.py) 的 `LlmResponder`——职责分离：planner 只选工具，回话交给 responder。`Agent` 在"本轮没调用任何工具"时用 responder 生成自然回复。`validate_final_action` 已守住边界，只有真闲聊才触发。
- **测试**：`tests/test_responder.py`。

### 7. ResponsePolicy 最终回复仲裁层（设计 + 实现 + 评审）
- **设计**：先完善了 [response-policy-design.md](docs/response-policy-design.md)——拦截档（clarification/safety，择一即停）/ 内容档（memory confirmation + tool summary + failure，可组合）/ 兜底档（planner answer / responder）；补失败档；降级 Pydantic/LangGraph 为"需权衡的岔路"。
- **实现**：[response_policy.py](agentic_core/response_policy.py) 的 `ResponsePolicy.decide()` 返回可审计的 `ResponseDecision(text, tiers, reason)`，进入 `result` 并被 trace_view 打印。依赖失败计算不写笔记的判断在 ResponsePolicy 和 `RuleBasedPlanner` 双重把关。
- **测试**：`tests/test_response_policy.py`（每档一个确定性用例）。

### 8. 修 ResponsePolicy 敏感检测的脆弱耦合
- **问题**：LLM 记忆路径的敏感拒绝只写 `{"confidence": N}`，没有 `sensitivity_risk`，safety 档只能靠 `"敏感" in reason` 子串匹配——文案一改就静默失效，且无测试覆盖。
- **修复**：`LlmMemoryPolicy` 敏感拒绝时写入稳定信号 `sensitivity_risk=5`（与规则版一致）；`ResponsePolicy._is_sensitive_memory_rejection` 只认结构化信号，删掉子串匹配。
- **测试**：LLM 路径写入信号 + ResponsePolicy 无关键词也能触发 safety。

### 9. 堵住敏感信息泄漏进 note.add / todo.add
- **问题**（评审后端到端验证时发现）：长期记忆拦住了，但 LLM planner 转头调 `note.add` 把密码原文写进笔记，还被回显。写入类工具没有敏感检查。
- **修复**：[registry.py](../agentic_core/tools/registry.py) 在**工具执行层**（所有工具调用的唯一入口）加守卫——`_register` 增加 `guard_sensitive` 标记，`note.add`/`todo.add` 标为 True；`execute()` 执行前检查输入，命中 `SENSITIVE_PATTERN` 就 `raise`（变失败 observation，不落地），拒绝信息不回显原文。`memory.add` 本就经 policy 网关，无需改。
- **测试**：`tests/test_tool_sensitive_guard.py`（拒绝且不落地、错误不回显）。

---

## 当前链路

```text
Agent.run(goal)
  -> SafetyPolicy(check)            # 请求级全局安全拦截,命中即拒绝整轮
  -> MemoryPolicy(evaluate)         # 规则版 或 LLM版(程序把关+敏感一票否决)
  -> [save active long-term memory] # 精确去重 + 规则语义合并 + 生命周期治理
  -> Plan-Act-Observe loop
       Planner(next)                # HermesPlanner(LLM) -> RuleBasedPlanner(兜底)
       MiddlewarePipeline           # 审批/成本/timeout/retry/tracing/idempotency
       ToolRegistry.execute         # 写入类工具敏感守卫;ToolSpec 治理元数据
       Observation
  -> ResponsePolicy.decide          # 拦截/组合/兜底分层,输出可审计 ResponseDecision
  -> Final Answer
```

可观测：`AGENTIC_TRACE=brief` 打印记忆决策(llm/fallback)、每步动作/工具结果、回退原因+模型原始输出、ResponseDecision 的 tiers/reason。`AGENTIC_EVENT_LOG=jsonl|sqlite` 可追加写入本地事件日志,`agentic_core.observability.event_log` 可按 runId 查看时间线。JSONL 写入默认启用同名 `.lock` 文件,保护大小轮转和追加写入;事件查看默认读取轮转备份,也可用 `--current-only` 只看当前文件。SQLite 后端使用 `data/events.db`,支持本地结构化查询。

新增模块：`memory_policy.LlmMemoryPolicy` / `response_policy` / `responder` / `trace_view` / `json_utils` / `event_payloads` / `event_writer` / `event_log` / `eval_dataset` / `eval_review` / `eval_diff` / `eval_history` / `eval_harness` / `middleware`。

文档结构：README 已收束为入口页,细节拆入 `docs/tutorial.md`、`docs/architecture.md`、`docs/operations.md`、`docs/evals.md`、`docs/acceptance-checklist.md`。七阶段完成度和生产缺口见 `docs/production-readiness-audit.md`。

Eval Harness：默认 8 个确定性用例,覆盖计算+笔记、长期记忆保存、记忆影响学习计划、安全拒绝、敏感记忆拒绝、技术栈追问、技术栈保存、计算失败不写笔记。报告包含 case pass rate、工具成功率、预期工具失败、事件计数、ResponsePolicy tiers 和质量门禁(`EvalThresholds`)。`eval_dashboard.py` 可把 eval report/history/dataset 聚合成本地 HTML/JSON 治理看板,`eval_server.py` 用标准库 `http.server` 提供 `/health`、`/dashboard`、`/api/dashboard`、`/api/rubrics`、`/api/reviews/status`、`/api/reviews/decisions` 和 `POST /api/reviews/apply`,并支持静态 admin/viewer/reviewer token、本地 signed claims token 和 tenant policy JSON scope: `eval.viewer` 可读,`eval.reviewer` 可写审核;写入 API 只使用服务端配置的 `--dataset` 与 `--review-output`,请求体不能指定文件路径,可通过 `--review-store` 把多人审核 decision 写入 SQLite,并可通过 `--audit-events` 写入 `eval_review_apply` / `eval_review_apply_failed` JSONL 审计事件。`auth_tokens.py` 可创建带 `sub/tenant/scopes/iat/exp` 的 HMAC signed claims token,`tenant_policy.py` 可检查 tenant 是否启用且允许当前 scope。`eval_replay.py` 可按 runId 从 JSONL/SQLite event log 生成 replay inspection bundle,`eval_dataset.py` 可从 JSONL/SQLite event log 生成待审核 dataset 草稿,`eval_sampling.py` 可按 review/judge/risk 原因生成优先级复核队列,`eval_review.py` 可批准/拒绝 case、写入 judge 人工 label、输出多用户 review state、统计多人复核 agreement,并输出 golden dataset,`eval_review_store.py` 可用 SQLite 保存/导入/分页查询 review decisions 并生成 review state,`eval_judge_registry.py` 可登记/校验 judge rubric 版本,`eval_harness --cases --require-reviewed` 可只跑已审核 dataset,`eval_diff.py` 可对比两次 JSON 报告并用 `--fail-on-regression` 接 CI,`eval_history.py` 可把报告追加到 JSONL 历史并输出趋势摘要,`eval_judge.py` 提供离线 rule judge 和可选 Ollama LLM judge 骨架。启用 judge 且 case 带 `expectedJudgeScore` / `expectedJudgePassed` 时,eval 会检查 score drift、label mismatch 和 rubric/version mismatch。

Middleware Pipeline：工具执行已统一进入 `MiddlewarePipeline.execute_tool()`。`RuntimeIdentity` 提供 user/tenant/roles/permission scopes 学习版身份上下文。`ToolSpec.timeoutMs`、`retryCount`、`costUnits`、`requiresApproval`、`permissionScope`、`sideEffect`、`riskLevel`、`owner`、`slaTier`、`dataClassification`、`auditClassification`、`externalSideEffect`、`inputJsonSchema` 和 lifecycle/migration 元数据会进入治理元数据,为审计、预算、幂等、文档和排障打基础。`ToolGovernancePolicy` 已支持 allowed/denied permission scopes、risk/side-effect 审批策略和 tenant+run 级 cost budget。`ToolBudgetStore` / `JsonFileToolBudgetStore` / `SQLiteToolBudgetStore` 已支持本地 JSON 文件和 SQLite 预算后端,可在同机 CLI/chat 进程间共享预算,SQLite 版本用事务保护 reserve。`IdempotencyStore` / `JsonFileIdempotencyStore` / `SQLiteIdempotencyStore` 已接入 write 工具成功结果缓存;read 工具不缓存,失败 write 不缓存,命中时短路返回第一次结果,并可在同机 CLI/chat 进程间共享幂等结果。`ToolOutputSafetyMiddleware` 会净化工具输出和错误中的敏感信息,避免进入最终回复、trace 或事件。`ToolTraceSink` / `InMemoryToolTraceSink` / `JsonlToolTraceSink` / `OtlpHttpToolTraceSink` 已接入 OTel-style 工具 span,覆盖成功、失败、审批短路和幂等命中路径;span 只保存治理元数据和状态,不保存工具输入/输出,可显式发送到 OTLP/HTTP collector。

Event Payload Schema：`event_payloads.py` 已定义核心事件的 required fields、typed payload dataclass 和标准库版 schema migration。Agent 主链路不再现场拼裸 dict 事件 payload;`MemoryStore.record_event()` 会在写入前迁移旧 payload、校验 payload,并将 `payloadSchema.valid/errors/migrationsApplied` 写进事件,兼容旧 dict 调用。`JsonMemoryStore` 读取旧 v1 事件时也会迁移到当前 payload schema。

环境开关：`AGENTIC_MODEL` / `AGENTIC_PLANNER` / `AGENTIC_MEMORY_POLICY` / `AGENTIC_MEMORY_LIFECYCLE_POLICY_PATH` / `AGENTIC_SAFETY_POLICY` / `AGENTIC_SAFETY_FAIL_CLOSED` / `AGENTIC_SAFETY_REVIEW_QUEUE` / `AGENTIC_SAFETY_REVIEW_QUEUE_PATH` / `AGENTIC_USER_ID` / `AGENTIC_TENANT_ID` / `AGENTIC_ROLES` / `AGENTIC_PERMISSION_SCOPES` / `AGENTIC_TRACE` / `AGENTIC_TOOL_TRACE_SINK` / `AGENTIC_TOOL_TRACE_ENDPOINT` / `AGENTIC_MEMORY_STORE` / `AGENTIC_MEMORY_PATH` / `AGENTIC_EVENT_LOG` / `AGENTIC_EVENT_LOG_PATH` / `AGENTIC_EVENT_LOG_MAX_BYTES` / `AGENTIC_EVENT_LOG_BACKUP_COUNT` / `AGENTIC_EVENT_LOG_LOCK`（+ 兼容 `AGENTIC_CHAT_DEBUG`）。

---

## 遗留 / 未做（按优先级）

- **[中] Memory Lifecycle 仍是规则版 + 本地向量检索边界**：已支持 `MemoryLifecyclePolicy` 单一策略源、JSON 增量策略配置、active/archived、user/tenant namespace、memory_admin 审核维护 CLI、memory review HTTP API、冲突检测/解决、访问统计、精确去重、技术栈/学习时长偏好的规则语义合并、importance、expiresAt、retention 归档、本地 `HashingMemoryEmbeddingIndex` 相似度检索;真实 embedding 语义合并、正式协作审核 UI、租户级策略中心仍未做。
- **[中] Event Log 后端仍是本地学习版**：已具备 EventWriter 抽象、JSONL、SQLite、大小轮转/备份保留、基础文件锁、轮转备份读取、timeline、eval 统计;Postgres/ClickHouse/OTel、集中式可观测平台、分布式级别并发治理未做。
- **[中] SafetyPolicy 已有生产化骨架**：已支持规则 checker、LLM checker、CompositeSafetyPolicy 多 checker 汇总、allow/warn/review/refuse 分级动作、fail-open/fail-closed 配置、结构化审计 metadata 和本地 SafetyReviewQueue;真实生产还应接外部 moderation、协作式人审平台、租户级安全策略和更完整的策略包。
- **[低] 文档持续维护**：README 已拆分为入口页 + tutorial / architecture / operations / evals / acceptance checklist;后续只需随功能演进同步更新。

已在后续修掉：

- `build_answer` 与 `ResponsePolicy` 的工具结果汇总重复已抽到 `tool_summary.summarize_tool_trace()`。
- 无工具意图的闲聊/纯记忆确认轮次已通过 `planner_skipped` 跳过 Planner,避免 HermesPlanner + LlmResponder 双 LLM 调用;无 responder 的离线 demo 仍保留 planner final answer。

---

## 测试

```bash
cd /Users/jurongchao/Desktop/ai学习测试库/agentic
.venv/bin/python -m pytest -q  # 330 passed
.venv/bin/python -m mypy agentic_core
python3 -m compileall agentic_core examples tests
python3 -m evalops.harness
python3 -m evalops.harness --json
```

LLM 相关全部用 stub client 覆盖，不依赖真实 Ollama；真实 Ollama 仅用于端到端手动验证。
