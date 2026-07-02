# Agentic Core Lab

这个目录用于实操 agentic 应用核心链路设计和实现。现在推荐用 Python 版本作为主线学习,JS 版本保留为 v0 参考。

Python 主线目标:

```text
User Input
  -> SafetyPolicy(有害请求直接拒绝整轮)
  -> MemoryPolicy(规则版 或 LLM版, 程序把关 + 敏感一票否决)
  -> [写入长期记忆]
  -> Plan-Act-Observe loop(Planner -> ToolRegistry[敏感守卫] -> Observation)
  -> ResponsePolicy(global_safety/拦截/组合/兜底, 仲裁最终回复)
  -> Final Answer
```

JS v0 目标:

```text
User Goal -> Context Builder -> Rule Planner -> Tool Executor -> Observation -> Memory -> Final Response
                                  ^                                               |
                                  |---------------- Re-plan Loop -----------------|
```

## Python 快速运行

确保 Ollama 正在运行,并且已经安装 `openhermes:latest`:

```bash
ollama list
```

运行 Python 主线:

```bash
cd /Users/jurongchao/Desktop/ai学习测试库/agentic
python3 -m agentic_core.cli "以后安排学习任务时，每次控制在30分钟以内"
python3 -m agentic_core.cli "帮我计算 128 * 7, 然后记录成学习笔记"
```

## 连续对话模式

CLI 入口是一句话运行一次。连续对话入口会启动一个长期运行的 Python 进程,多轮输入共用同一个 `MemoryStore`:

```bash
python3 -m agentic_core.chat
```

示例:

```text
User>
以后安排学习任务时，每次控制在30分钟以内
Agent> ...

User>
帮我计算 128 * 7, 然后记录成学习笔记
Agent> ...
```

退出:

```text
exit
quit
退出
```

查看每轮的执行过程。`AGENTIC_TRACE` 控制过程可见度:

- `brief`(chat 默认): 可读的分步 trace,能看到记忆决策走的 llm 还是回退、每一步动作/工具结果,以及**回退时的原因和模型原始输出**。
- `json`: 完整 JSON(memoryDecision + trace + snapshot)。
- `off`: 只看最终答案和记忆摘要。

```bash
AGENTIC_TRACE=json python3 -m agentic_core.chat
AGENTIC_TRACE=off python3 -m agentic_core.chat
```

`AGENTIC_CHAT_DEBUG=1` 仍可用,等价于 `AGENTIC_TRACE=json`。cli 默认 `json`,可用 `AGENTIC_TRACE=brief` 切换到可读分步。

如果你的终端中文输入/删除显示不稳定,默认会使用两行输入模式: 先显示 `User>`,下一行再输入正文。也可以自定义提示符:

```bash
AGENTIC_CHAT_PROMPT='You> ' python3 -m agentic_core.chat
```

如果想恢复单行输入:

```bash
AGENTIC_CHAT_INLINE_PROMPT=1 python3 -m agentic_core.chat
```

默认情况下,记忆只在本次 Python 进程内共享。若要跨 CLI/chat 进程保留 notes、todos、longTermMemories,开启 JSON 记忆持久化:

```bash
AGENTIC_MEMORY_STORE=json python3 -m agentic_core.cli "以后安排学习任务时，每次控制在30分钟以内"
AGENTIC_MEMORY_STORE=json python3 -m agentic_core.chat
```

默认文件路径是 `data/memory.json`,也可以自定义:

```bash
AGENTIC_MEMORY_STORE=json AGENTIC_MEMORY_PATH=data/my-memory.json python3 -m agentic_core.chat
```

`data/memory.json` 是运行时产物,已加入 `.gitignore`。

事件日志默认也只保存在进程内。若要把每轮事件追加写入本地 JSONL,开启:

```bash
AGENTIC_EVENT_LOG=jsonl python3 -m agentic_core.cli "帮我计算 128 * 7, 然后记录成学习笔记"
AGENTIC_EVENT_LOG=jsonl AGENTIC_EVENT_LOG_PATH=data/events.jsonl python3 -m agentic_core.chat
```

`data/*.jsonl` 是运行时产物,已加入 `.gitignore`。事件写入前会复用 MemoryPolicy 的敏感信息规则做脱敏;JSONL 写入失败不会打断用户主流程。JSONL 写入默认会创建同名 `.lock` 文件,把大小轮转和追加写入放进同一个文件锁里,降低 CLI/chat 多进程同时写入时的错位风险。事件查看工具默认会同时读取 `events.jsonl.1`、`events.jsonl.2` 等轮转备份。

当前事件日志覆盖一轮 run 的核心生命周期:

```text
run_started
safety_decision / safety_refusal
memory_decision / memory_saved / memory_clarification
planner_action / planner_fallback
planner_skipped
tool_started / tool_observation
response_decision
run_completed / run_failed
```

查看 JSONL 里的运行记录:

```bash
python3 -m agentic_core.event_log --path data/events.jsonl
python3 -m agentic_core.event_log --path data/events.jsonl --run-id run_123
python3 -m agentic_core.event_log --path data/events.jsonl --current-only
```

运行确定性 eval harness:

```bash
python3 -m agentic_core.eval_harness
python3 -m agentic_core.eval_harness --json
```

当前 eval 使用规则 planner/policy,不依赖 Ollama。它会覆盖计算+笔记、长期记忆保存、记忆影响学习计划、安全拒绝、敏感记忆拒绝,并统计工具成功率、planner fallback、safety refusal、memory saved、run failed 和平均 step 数。

切换组件(环境变量):

- `AGENTIC_PLANNER=hermes|rule`(默认 hermes): LLM planner 或纯规则 planner。
- `AGENTIC_MEMORY_POLICY=llm|rule`(默认 llm): LLM 抽取记忆策略 或纯规则版。
- `AGENTIC_MODEL=openhermes:latest`: Ollama 模型名。
- `AGENTIC_TRACE=off|brief|json`: 过程可见度(见上文)。
- `AGENTIC_EVENT_LOG=memory|jsonl`: 事件日志后端。默认 memory,不落盘。
- `AGENTIC_EVENT_LOG_PATH=data/events.jsonl`: JSONL 事件日志路径。
- `AGENTIC_EVENT_LOG_MAX_BYTES=10485760`: JSONL 单文件最大字节数;设置后启用大小轮转。
- `AGENTIC_EVENT_LOG_BACKUP_COUNT=3`: JSONL 轮转备份数量;设为 0 时只截断当前文件。
- `AGENTIC_EVENT_LOG_LOCK=1|0`: JSONL 文件锁。默认开启;锁文件名为 `events.jsonl.lock`。
- `AGENTIC_MEMORY_STORE=memory|json`: 记忆后端。默认 memory,退出进程即消失。
- `AGENTIC_MEMORY_PATH=data/memory.json`: JSON 记忆文件路径。

HermesPlanner 和 LlmMemoryPolicy 会对 Ollama 请求使用 `format: "json"`,减少本地模型输出 markdown/解释文字导致的 fallback。注意这只是降低噪声,不是安全边界;程序仍会解析、校验工具名/参数/记忆类型/敏感信息。

`AGENTIC_PLANNER=rule` + `AGENTIC_MEMORY_POLICY=rule` 可完全离线运行(不依赖 Ollama):

```bash
AGENTIC_PLANNER=rule AGENTIC_MEMORY_POLICY=rule python3 -m agentic_core.cli "帮我计算 128 * 7, 然后记录成学习笔记"
AGENTIC_MODEL=openhermes:latest python3 -m agentic_core.cli "添加待办: 学习 agentic 核心链路, 然后列出待办"
```

记忆影响规划示例:

```bash
AGENTIC_PLANNER=rule AGENTIC_MEMORY_POLICY=rule AGENTIC_MEMORY_STORE=json python3 -m agentic_core.cli "以后安排学习任务时，每次控制在30分钟以内"
AGENTIC_PLANNER=rule AGENTIC_MEMORY_POLICY=rule AGENTIC_MEMORY_STORE=json python3 -m agentic_core.cli "帮我安排 agentic memory 的学习计划"
```

第二条会读取第一条保存的长期偏好,并调用 `study.plan` 生成不超过 30 分钟的学习计划。

记忆策略 demo:

```bash
python3 examples/run_memory_demo.py
```

## Python 模块职责

```text
agentic_core/
  cli.py              # 单次运行入口
  chat.py             # 连续对话入口(多轮共享 MemoryStore)
  agent.py            # Plan-Act-Observe loop 编排
  contracts.py        # 角色 Protocol 契约 + dataclass PlannerContext
  ollama_client.py    # Ollama /api/chat 调用
  planner.py          # HermesPlanner(LLM) + RuleBasedPlanner(兜底)
  memory_policy.py    # RuleBasedMemoryPolicy + LlmMemoryPolicy(抽取+把关)
  memory.py           # MemoryStore / JsonMemoryStore, notes/todos/events/long_term_memories
  event_writer.py     # EventWriter 抽象 + Memory/JSONL/Composite 实现
  event_log.py        # JSONL 事件读取 + 按 runId 时间线查看
  eval_harness.py     # 确定性 eval 用例 + 行为指标统计
  middleware.py       # 工具执行中间件管道(审批/成本等横切逻辑)
  tools.py            # calculator/note/todo/study.plan/memory.add + ToolSpec 治理元数据
  responder.py        # 无工具时的自然语言回复(LlmResponder)
  response_policy.py  # 最终回复仲裁(global_safety/拦截/组合/兜底)
  safety_policy.py    # 请求级全局安全拦截(RuleBasedSafetyPolicy)
  trace_view.py       # 可读分步 trace 渲染
  json_utils.py       # 从模型输出里抽 JSON(planner/policy 共享)
  schemas.py          # Typed State: Action/TraceStep/MemorySnapshot/AgentRunResult 等数据结构
```

## MemoryPolicy(记忆策略)

核心原则: 不是用户说的每句话都值得进长期记忆,且**敏感信息一票否决**(密码/密钥/证件号等,由程序侧正则拦截,不依赖模型)。两个实现共用 `evaluate(text) -> MemoryDecision` 契约,用 `AGENTIC_MEMORY_POLICY` 切换:

- `RuleBasedMemoryPolicy`(纯规则,确定性,离线): 按维度打分——`future_relevance` / `stability` / `user_preference` / `task_continuity` / `explicit_memory_intent` / `user_profile`,正向分达阈值 7 且敏感风险不高才保存。也是 LLM 版的兜底。
- `LlmMemoryPolicy`(默认): LLM 做语义抽取,程序做最终把关(敏感一票否决 + 置信度阈值 + 类型校验);Ollama 不可用或输出非法时回退规则版。

`MemoryStore.add_long_term_memory()` 会对同类型、同正文的 active 长期记忆做精确去重。长期记忆有基础生命周期字段:

- `status`: `active` / `archived`。只有 active 记忆会进入 planner snapshot。
- `updatedAt`: 最近更新或去重命中的时间。
- `lastAccessedAt` / `accessCount`: 记忆被提供给 Planner 时自动更新。
- `archivedAt` / `archiveReason`: 归档记录,归档不是删除。

语义相似合并、过期策略和重要性排序留给后续 Memory Lifecycle 阶段继续增强。

示例:

- “我今天有点累”: 临时状态,不保存。
- “以后安排学习任务时，每次控制在30分钟以内”: 长期偏好,保存。
- “我的密码是 …”: 敏感,拒绝保存(也不会写进 note/todo)。

## 最终回复与安全

- **global safety(请求级拦截)**: `SafetyPolicy` 在最前面判断整轮请求是否有害;命中即拒绝,**跳过记忆评估和整个 loop**。当前规则版输出结构化 `SafetyDecision`,包含 `category`、`riskLevel`、`confidence`、`matchedRule`、`action` 和 `metadata`;LLM/moderation 版可经 `SafetyPolicy` 协议 drop-in。
- **ResponsePolicy** 仲裁最终回复,不让 responder 覆盖已发生的系统事实。优先级:
  - 拦截档(命中即停): global_safety(拒绝整轮)> clarification(追问)> local_safety(拒绝保存敏感信息)。
  - 内容档(可组合): 记忆确认、工具结果汇总、失败/未完成说明。
  - 兜底档: planner 的 final answer、普通闲聊交给 `LlmResponder`。
- **闲聊也能答**: 明显没有工具意图的轮次会记录 `planner_skipped`,跳过 Planner,由 `LlmResponder` 用自然语言回复。没有 responder 的离线 demo 仍保留 planner final answer。
- **敏感信息不落地**: 长期记忆、note.add、todo.add、memory.add 都过同一份 `SENSITIVE_PATTERN`,不管被路由到哪条路。
- **Tool Metadata**: 每个工具都暴露 `permissionScope`、`sideEffect`、`timeoutMs`、`costUnits`、`riskLevel`、`requiresApproval`、`guardSensitive`、`version`。当前阶段先作为治理元数据进入 registry,后续可被 middleware / safety / approval 直接消费。
- **Middleware Pipeline**: `Agent` 在工具执行前后经过 `MiddlewarePipeline`。默认启用 `ApprovalMiddleware` 和 `CostAccountingMiddleware`: 需要审批但未批准的工具会被短路为失败 observation;成本中间件先记录 `costUnits`,后续可接预算/审计。

## 开发与测试

本机 externally-managed,用 venv:

```bash
python3 -m venv .venv && .venv/bin/pip install pytest mypy
.venv/bin/python -m pytest -q     # 事件日志/Typed State/策略等测试
.venv/bin/python -m mypy          # 类型检查(门禁锁 agentic_core)
```

CI: `.github/workflows/ci.yml` 每次 push / PR 自动跑 mypy + pytest。

## JS v0 快速运行

JS 版本不依赖真实 LLM API,用于理解最小规则型 loop:

```text
User Goal -> Context Builder -> Planner -> Tool Executor -> Observation -> Memory -> Final Response
                                  ^                                            |
                                  |---------------- Re-plan Loop --------------|
```

```bash
cd agentic
npm run demo
npm run demo:todo
node src/index.js "帮我计算 23 + 19, 并记录为笔记"
```

## 核心链路

1. **输入标准化**
   - 接收用户目标。
   - 生成 run id。
   - 初始化 trace,便于后续观测和调试。

2. **上下文构建**
   - 读取短期记忆和长期记忆。
   - 注入可用工具清单。
   - 构造 planner 需要的状态。

3. **计划生成**
   - planner 根据目标、记忆、已完成步骤决定下一步。
   - 输出结构化 action,例如 `tool` 或 `final`。

4. **工具执行**
   - tool registry 校验工具名和参数。
   - executor 调用工具并捕获成功/失败结果。
   - 结果作为 observation 写回 loop。

5. **观察与再规划**
   - planner 读取 observations。
   - 若目标未完成,继续下一轮。
   - 若目标完成,输出 final answer。

6. **记忆沉淀**
   - 将关键 observation 写入 memory。
   - trace 保留每次 action、observation、耗时和错误。

## 仓库结构

```text
agentic/
  agentic_core/     # Python 主线(见上文模块职责)
  tests/            # pytest 测试
  examples/         # run_memory_demo.py
  docs/             # 设计与开发文档(见下)
  src/              # JS v0 参考实现(不再维护)
  .github/workflows/ci.yml
  pyproject.toml    # pytest + mypy 配置
```

文档:

- `docs/development-log.md` — 加固开发日志 + 遗留清单(路线图)。
- `docs/response-policy-design.md` — 回复策略设计。
- `docs/next-implementation-plan.md` — 后续阶段计划(持久化等)。
- `docs/core-link.md` — 早期设计说明。

## 下一步可扩展方向

详见 `docs/development-log.md` 的遗留清单,要点:

- 记忆持久化(内存 -> JSON/SQLite)、记忆去重。
- 记忆后端继续演进(JSON -> SQLite/Postgres/Obsidian)、语义级记忆合并/过期/归档。
- LLM 输出用 Ollama `format:json` 降低回退率。
- global safety(请求级安全拦截)。
- 生产化: Protocol 显式契约 + 中间件管道 + typed state。
