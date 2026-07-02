# Agentic Core Lab

这个目录用于实操 agentic 应用核心链路设计和实现。现在推荐用 Python 版本作为主线学习,JS 版本保留为 v0 参考。

Python 主线目标:

```text
User Input -> MemoryPolicy -> Hermes/Ollama Planner -> Tool Executor -> Observation -> MemoryStore -> Final Answer
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

切换 Planner:

```bash
AGENTIC_PLANNER=rule python3 -m agentic_core.cli "帮我计算 128 * 7, 然后记录成学习笔记"
AGENTIC_MODEL=openhermes:latest python3 -m agentic_core.cli "添加待办: 学习 agentic 核心链路, 然后列出待办"
```

记忆策略 demo:

```bash
python3 examples/run_memory_demo.py
```

## Python 模块职责

```text
agentic_core/
  cli.py              # CLI 入口
  agent.py            # Plan-Act-Observe loop
  ollama_client.py    # Ollama /api/chat 调用
  planner.py          # HermesPlanner + RuleBasedPlanner fallback
  memory.py           # notes/todos/events/long_term_memories
  memory_policy.py    # 长期记忆维度评分与保存决策
  tools.py            # calculator/note/todo/memory tools
  schemas.py          # action/observation/memory decision 数据结构
```

## MemoryPolicy

`MemoryPolicy` 不把所有用户输入都写入长期记忆,而是按维度评分:

- `future_relevance`: 是否影响未来决策。
- `stability`: 是否长期稳定。
- `user_preference`: 是否表达偏好、约束、习惯。
- `task_continuity`: 是否帮助未来任务延续。
- `sensitivity_risk`: 是否包含敏感或不适合保存的信息。

默认正向分达到 7 且敏感风险不高才保存。

示例:

- “我今天有点累”: 临时状态,不保存。
- “以后安排学习任务时，每次控制在30分钟以内”: 长期偏好,保存。

## JS v0 快速运行

JS 版本不依赖真实 LLM API,用于理解最小规则型 loop:

```text
User Goal -> Context Builder -> Planner -> Tool Executor -> Observation -> Memory -> Final Response
                                  ^                                            |
                                  |---------------- Re-plan Loop --------------|
```

## 快速运行

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

## 文件结构

```text
agentic/
  src/
    agent.js       # Agent loop 编排
    index.js       # CLI 入口
    memory.js      # 简单内存存储
    planner.js     # 可替换 planner
    tools.js       # 工具注册与执行
  docs/
    core-link.md   # 设计说明
```

## 下一步可扩展方向

- 把 `src/planner.js` 替换成真实 LLM planner。
- 将 `MemoryStore` 从内存换成 SQLite/Postgres/Redis。
- 为 tool execution 增加权限、超时、重试和审计。
- 增加 human-in-the-loop 审批节点。
- 增加 evaluator,让 agent 对最终结果做自检。
