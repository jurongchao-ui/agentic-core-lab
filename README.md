# Agentic Core Lab

这个目录用于实操 agentic 应用核心链路设计和实现。当前推荐用 Python 版本作为主线学习,JS 版本保留为 v0 参考。

## 核心链路

```text
User Input
  -> SafetyPolicy
  -> MemoryPolicy
  -> Planner
  -> MiddlewarePipeline
  -> ToolRegistry
  -> Observation
  -> MemoryStore / EventLog
  -> ResponsePolicy
  -> Final Answer
```

## 快速运行

进入项目:

```bash
cd /Users/jurongchao/Desktop/ai学习测试库/agentic
```

完全离线规则模式:

```bash
AGENTIC_PLANNER=rule AGENTIC_MEMORY_POLICY=rule python3 -m agentic_core.cli "帮我计算 128 * 7"
```

默认模式会优先使用 Ollama/Hermes:

```bash
python3 -m agentic_core.cli "以后安排学习任务时，每次控制在30分钟以内"
python3 -m agentic_core.cli "帮我计算 128 * 7, 然后记录成学习笔记"
```

连续对话:

```bash
python3 -m agentic_core.chat
```

运行 eval:

```bash
python3 -m agentic_core.eval_harness
python3 -m agentic_core.eval_harness --json
```

审核/维护长期记忆:

```bash
python3 -m agentic_core.memory_admin list --path data/memory.json --user-id local_user --tenant-id default_tenant
python3 -m agentic_core.memory_admin archive --path data/memory.json --memory-id memory_1 --reason "人工审核归档"
python3 -m agentic_core.memory_admin set-importance --path data/memory.json --memory-id memory_1 --importance 80
python3 -m agentic_core.memory_admin conflicts --path data/memory.json --user-id local_user --tenant-id default_tenant
python3 -m agentic_core.memory_admin resolve-conflict --path data/memory.json --keep-memory-id memory_2 --reason "保留最新长期记忆"
```

启动只读本地 eval 治理服务:

```bash
python3 -m agentic_core.eval_server \
  --report data/eval-current.json \
  --history data/eval-history.jsonl \
  --dataset data/eval-golden.json \
  --review-output data/eval-golden-reviewed.json \
  --review-store data/reviews.db \
  --audit-events data/eval-server-audit.jsonl \
  --port 8765
```

可选开启 Bearer Token:

```bash
AGENTIC_EVAL_SERVER_TOKEN=local-secret \
python3 -m agentic_core.eval_server \
  --dataset data/eval-dataset.json \
  --review-output data/eval-golden.json \
  --review-store data/reviews.db \
  --audit-events data/eval-server-audit.jsonl
```

也可以拆分只读和审核 token:

```bash
AGENTIC_EVAL_SERVER_VIEWER_TOKEN=view-secret \
AGENTIC_EVAL_SERVER_REVIEWER_TOKEN=review-secret \
python3 -m agentic_core.eval_server --dataset data/eval-dataset.json --review-output data/eval-golden.json --review-store data/reviews.db
```

也可以使用本地 signed claims token:

```bash
python3 -m agentic_core.auth_tokens create \
  --secret local-signing-secret \
  --subject reviewer_1 \
  --tenant default_tenant \
  --scopes eval.viewer,eval.reviewer \
  --ttl 3600

AGENTIC_EVAL_SERVER_SIGNING_SECRET=local-signing-secret \
AGENTIC_EVAL_SERVER_TENANT_POLICY=data/tenant-policy.json \
python3 -m agentic_core.eval_server --dataset data/eval-dataset.json --review-output data/eval-golden.json --review-store data/reviews.db
```

## 文档导航

- [Tutorial](docs/tutorial.md): 从 CLI/chat、记忆持久化、事件日志到 eval 的实操教程。
- [Architecture](docs/architecture.md): Typed State、Memory、Safety、Tool Governance、Event Log 的架构说明。
- [Operations](docs/operations.md): 环境变量、运行模式、可观测性、安全和测试门禁。
- [Evals](docs/evals.md): 默认 8 个 eval 用例、报告结构、指标和 gate。
- [Acceptance Checklist](docs/acceptance-checklist.md): 当前验收清单、冒烟命令和仍未等同完整生产的部分。
- [Production Readiness Audit](docs/production-readiness-audit.md): 按 7 个生产化阶段逐项审计完成度、证据和缺口。
- [Development Log](docs/development-log.md): 阶段性加固记录和遗留路线图。
- [Response Policy Design](docs/response-policy-design.md): 最终回复策略设计。
- [Persistent Event Log Plan](docs/persistent-event-log-production-plan.md): 事件日志生产级方案。
- [Typed State Refactor Plan](docs/typed-state-refactor-plan.md): Typed State 改造方案。

## 当前能力

- Typed State 主链路,`Agent.run_typed()` 返回 `AgentRunResult`。
- CLI 单轮运行和 Chat 连续对话。
- 规则版与 LLM 版 MemoryPolicy,敏感信息一票否决。
- Memory Lifecycle: `MemoryLifecyclePolicy` 单一策略源、active/archived、user/tenant namespace、memory_admin 审核维护 CLI、冲突检测/解决、访问统计、重要性、过期归档、规则语义合并。
- JSON 记忆持久化。
- RuleBased / LLM / Composite SafetyPolicy。
- ResponsePolicy 最终回复仲裁。
- RuntimeIdentity: user/tenant/roles/permission scopes 学习版身份上下文。
- ToolSpec 治理元数据 + ToolGovernancePolicy。
- MiddlewarePipeline: approval、cost、timeout、retry、idempotency、tracing metadata。
- Event payload schema: typed payload dataclass + 写入前 payloadSchema 校验。
- EventWriter 抽象 + JSONL/SQLite 持久事件日志、脱敏、轮转、文件锁、备份读取。
- Eval Harness: 8 个确定性用例、本地治理 dashboard、带 viewer/reviewer RBAC、signed claims token、tenant policy JSON、本地静态 token、受保护 review 写入 API、review status API、review decisions 分页 API、SQLite review store 和 JSONL 审计事件的 governance server、replay inspection bundle、dataset 审核、复核队列采样、多人复核状态/一致性统计、judge registry/version 治理、judge 人工 label 校准、event-log-to-eval 草稿、报告 diff、历史趋势、rule/LLM judge、指标、事件计数和质量门禁。

## 当前验收状态

```text
pytest: 275 passed
mypy: success
compileall: passed
eval harness: 8/8 passed, Gate PASS
```

常用门禁:

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m mypy agentic_core
python3 -m compileall agentic_core examples tests
python3 -m agentic_core.eval_harness
```

## 仓库结构

```text
agentic/
  agentic_core/     # Python 主线
  tests/            # pytest 测试
  examples/         # run_memory_demo.py
  docs/             # 设计、运维、eval 和验收文档
  src/              # JS v0 参考实现
  pyproject.toml    # pytest + mypy 配置
```

## JS v0

JS 版本不依赖真实 LLM API,用于理解最小规则型 loop:

```bash
npm run demo
npm run demo:todo
node src/index.js "帮我计算 23 + 19, 并记录为笔记"
```
