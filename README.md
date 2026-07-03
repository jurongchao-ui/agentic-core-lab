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
- Memory Lifecycle: active/archived、访问统计、重要性、过期归档、规则语义合并。
- JSON 记忆持久化。
- RuleBased / LLM / Composite SafetyPolicy。
- ResponsePolicy 最终回复仲裁。
- RuntimeIdentity: user/tenant/roles/permission scopes 学习版身份上下文。
- ToolSpec 治理元数据 + ToolGovernancePolicy。
- MiddlewarePipeline: approval、cost、timeout、retry、idempotency、tracing metadata。
- EventWriter 抽象 + JSONL/SQLite 持久事件日志、脱敏、轮转、文件锁、备份读取。
- Eval Harness: 8 个确定性用例、指标、事件计数和质量门禁。

## 当前验收状态

```text
pytest: 152 passed
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
