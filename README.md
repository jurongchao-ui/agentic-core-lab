# Agentic Core Lab

这个目录用于实操 agentic 应用核心链路设计和实现。现在推荐用 Python 版本作为主线学习,JS 版本保留为 v0 参考。

Python 主线目标:

```text
User Input
  -> MemoryPolicy(规则版 或 LLM版, 程序把关 + 敏感一票否决)
  -> [写入长期记忆]
  -> Plan-Act-Observe loop(Planner -> ToolRegistry[敏感守卫] -> Observation)
  -> ResponsePolicy(拦截/组合/兜底, 仲裁最终回复)
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

注意: 当前阶段的记忆只在本次 chat 进程内共享。退出 `chat.py` 后,内存里的 notes、todos、longTermMemories 仍会消失。下一阶段会把记忆持久化到 `data/memory.json`。

切换组件(环境变量):

- `AGENTIC_PLANNER=hermes|rule`(默认 hermes): LLM planner 或纯规则 planner。
- `AGENTIC_MEMORY_POLICY=llm|rule`(默认 llm): LLM 抽取记忆策略 或纯规则版。
- `AGENTIC_MODEL=openhermes:latest`: Ollama 模型名。
- `AGENTIC_TRACE=off|brief|json`: 过程可见度(见上文)。

`AGENTIC_PLANNER=rule` + `AGENTIC_MEMORY_POLICY=rule` 可完全离线运行(不依赖 Ollama):

```bash
AGENTIC_PLANNER=rule AGENTIC_MEMORY_POLICY=rule python3 -m agentic_core.cli "帮我计算 128 * 7, 然后记录成学习笔记"
AGENTIC_MODEL=openhermes:latest python3 -m agentic_core.cli "添加待办: 学习 agentic 核心链路, 然后列出待办"
```

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
  ollama_client.py    # Ollama /api/chat 调用
  planner.py          # HermesPlanner(LLM) + RuleBasedPlanner(兜底)
  memory_policy.py    # RuleBasedMemoryPolicy + LlmMemoryPolicy(抽取+把关)
  memory.py           # notes/todos/events/long_term_memories
  tools.py            # calculator/note/todo/memory 工具 + 敏感守卫 + schema 真相源
  responder.py        # 无工具时的自然语言回复(LlmResponder)
  response_policy.py  # 最终回复仲裁(拦截/组合/兜底)
  trace_view.py       # 可读分步 trace 渲染
  json_utils.py       # 从模型输出里抽 JSON(planner/policy 共享)
  schemas.py          # Action/Observation/MemoryDecision 数据结构
```

## MemoryPolicy(记忆策略)

核心原则: 不是用户说的每句话都值得进长期记忆,且**敏感信息一票否决**(密码/密钥/证件号等,由程序侧正则拦截,不依赖模型)。两个实现共用 `evaluate(text) -> MemoryDecision` 契约,用 `AGENTIC_MEMORY_POLICY` 切换:

- `RuleBasedMemoryPolicy`(纯规则,确定性,离线): 按维度打分——`future_relevance` / `stability` / `user_preference` / `task_continuity` / `explicit_memory_intent` / `user_profile`,正向分达阈值 7 且敏感风险不高才保存。也是 LLM 版的兜底。
- `LlmMemoryPolicy`(默认): LLM 做语义抽取,程序做最终把关(敏感一票否决 + 置信度阈值 + 类型校验);Ollama 不可用或输出非法时回退规则版。

示例:

- “我今天有点累”: 临时状态,不保存。
- “以后安排学习任务时，每次控制在30分钟以内”: 长期偏好,保存。
- “我的密码是 …”: 敏感,拒绝保存(也不会写进 note/todo)。

## 最终回复与安全

- **ResponsePolicy** 仲裁最终回复,不让 responder 覆盖已发生的系统事实。优先级分三类:
  - 拦截档(命中即停): clarification(追问)、local_safety(拒绝保存敏感信息)。
  - 内容档(可组合): 记忆确认、工具结果汇总、失败/未完成说明。
  - 兜底档: planner 的 final answer、普通闲聊交给 `LlmResponder`。
- **闲聊也能答**: 本轮没调用任何工具时,由 `LlmResponder` 用自然语言回复(不再是空模板)。
- **敏感信息不落地**: 长期记忆、note.add、todo.add、memory.add 都过同一份 `SENSITIVE_PATTERN`,不管被路由到哪条路。

## 开发与测试

本机 externally-managed,用 venv:

```bash
python3 -m venv .venv && .venv/bin/pip install pytest mypy
.venv/bin/python -m pytest -q     # 38 个测试
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
  tests/            # pytest, 38 个测试
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
- LLM 输出用 Ollama `format:json` 降低回退率。
- global safety(请求级安全拦截)。
- 生产化: Protocol 显式契约 + 中间件管道 + typed state。
