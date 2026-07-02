# Agentic Core Lab 下一阶段实施计划

## Summary

当前项目已经完成了 agentic 应用的最小核心链路:

```text
User Input -> MemoryPolicy -> Planner -> Tool Executor -> Observation -> MemoryStore -> Final Answer
```

下一阶段目标是把它从“一次命令跑一个 demo”升级为“可连续对话、可持久保存记忆、能让记忆影响规划”的小型 agentic 聊天系统。

优先级顺序:

1. 增加连续对话模式 `chat.py`
2. 增加 JSON 文件持久化记忆
3. 让 Planner 更明显地使用长期记忆
4. 增加真实学习计划工具 `study.plan`
5. 优化 trace 展示
6. 记录 Hermes 原始输出和 fallback 原因

## Current State

现在的 CLI 运行方式是:

```bash
python3 -m agentic_core.cli "以后安排学习任务时，每次控制在30分钟以内"
```

这意味着每次运行都会重新创建:

```python
memory = MemoryStore()
agent = Agent(...)
```

所以当前行为是:

```text
一句话 -> 启动一次 Python -> 创建空 MemoryStore -> 跑完 -> 程序退出 -> 记忆消失
```

这适合教学最小闭环,但还不像真实聊天机器人。

真实聊天 agent 应该更像:

```text
启动一次服务/进程
  -> 创建或加载 MemoryStore
  -> 用户第 1 句话 -> agent.run(...)
  -> 用户第 2 句话 -> agent.run(...)
  -> 用户第 3 句话 -> agent.run(...)
  -> 多轮共享同一份 memory
```

## Phase 1: 连续对话模式

### Goal

新增一个 REPL 聊天入口:

```bash
python3 -m agentic_core.chat
```

运行后进入:

```text
Agentic Core Chat
输入 exit / quit 退出

你: 以后安排学习任务时，每次控制在30分钟以内
Agent: 已保存你的学习任务偏好。

你: 帮我安排明天的 agentic 学习计划
Agent: 我会按每次 30 分钟以内拆分...
```

### Implementation

新增文件:

```text
agentic_core/chat.py
```

核心结构:

```python
memory = MemoryStore()
memory_policy = MemoryPolicy()
tools = ToolRegistry(memory)
planner = HermesPlanner(...)
agent = Agent(...)

while True:
    user_message = input("你: ").strip()
    if user_message in {"exit", "quit"}:
        break
    result = agent.run(user_message)
    print(result["answer"])
```

关键点:

- `memory = MemoryStore()` 必须放在 `while True` 外面。
- 这样多轮对话才能共享同一个 memory。
- 默认仍使用 HermesPlanner。
- 支持 `AGENTIC_PLANNER=rule` 切换规则模式。
- 支持 `AGENTIC_MODEL=openhermes:latest` 切换模型。

### Acceptance Criteria

同一个 chat 进程里:

1. 用户输入:

   ```text
   以后安排学习任务时，每次控制在30分钟以内
   ```

2. 系统保存长期记忆。
3. 用户继续输入:

   ```text
   帮我安排 agentic 学习计划
   ```

4. 第二轮可以在 memory snapshot 中看到第一轮保存的偏好。

## Phase 2: JSON 文件持久化记忆

### Goal

让记忆不再只存在 Python 进程内存中,而是写入本地文件:

```text
agentic/data/memory.json
```

实现后:

```text
第一次运行保存偏好
程序退出
第二次启动
仍然能读回之前的偏好
```

### Implementation

新增或扩展:

```text
agentic_core/memory.py
```

增加一个文件版 MemoryStore,例如:

```python
class JsonMemoryStore(MemoryStore):
    def __init__(self, path: Path) -> None:
        self.path = path
        super().__init__()
        self.load()

    def load(self) -> None:
        ...

    def save(self) -> None:
        ...
```

保存结构:

```json
{
  "notes": [],
  "todos": [],
  "events": [],
  "long_term_memories": []
}
```

写入策略:

- `add_note` 后自动 `save()`
- `add_todo` 后自动 `save()`
- `add_long_term_memory` 后自动 `save()`
- `record_event` 后可以保存最近事件,但事件过多时后续要裁剪

### Acceptance Criteria

1. 删除或清空 `agentic/data/memory.json`。
2. 运行 chat 或 CLI 保存一条长期偏好。
3. 退出程序。
4. 再次启动程序。
5. memory snapshot 中仍能看到之前保存的长期偏好。

## Phase 3: Planner 使用长期记忆

### Goal

现在长期记忆已经会保存,但对规划的影响还不够明显。

下一步要让 Planner 在处理新任务时明确参考:

```python
context["memory"].snapshot()["longTermMemories"]
```

例如已经保存:

```text
用户偏好: 以后安排学习任务时，每次控制在30分钟以内
```

当用户说:

```text
帮我安排 agentic 学习计划
```

系统应该输出或调用工具时体现:

```text
每个学习任务控制在 30 分钟以内
```

### Implementation

更新 `planner.py`:

- `HermesPlanner._messages()` 中强调长期记忆是规划约束。
- `RuleBasedPlanner` 增加一个简单的学习计划识别逻辑。
- 从 memory snapshot 中提取 `30分钟以内` 这类偏好。

### Acceptance Criteria

1. 先保存 30 分钟学习偏好。
2. 再请求学习计划。
3. 最终回答里必须出现“30 分钟”或等价约束。

## Phase 4: 新增 study.plan 工具

### Goal

增加一个更像真实业务的工具:

```text
study.plan
```

它根据主题和时间限制生成学习计划。

### Input

```python
{
    "topic": "agentic memory",
    "max_minutes": 30
}
```

### Output

```python
{
    "topic": "agentic memory",
    "maxMinutes": 30,
    "steps": [
        "10 分钟复习 MemoryPolicy",
        "10 分钟运行 memory demo",
        "10 分钟修改一个评分规则"
    ]
}
```

### Implementation

更新:

```text
agentic_core/tools.py
agentic_core/planner.py
```

ToolRegistry 新增:

```python
"study.plan"
```

Planner 新增识别:

```text
学习计划 / 学习安排 / study plan
```

并从长期记忆里读取 `max_minutes` 偏好。

### Acceptance Criteria

用户先说:

```text
以后安排学习任务时，每次控制在30分钟以内
```

再说:

```text
帮我安排 agentic memory 的学习计划
```

期望调用:

```text
study.plan
```

并生成不超过 30 分钟的计划。

## Phase 5: 简洁 Trace 展示

### Goal

当前 trace 是完整 JSON,适合调试,但初学者阅读压力大。

新增简洁展示:

```text
Step 1
Planner: rule_fallback
Action: calculator
Input: 128 * 7
Observation: 896

Step 2
Planner: rule
Action: note.add
Input: 计算 128 * 7 = 896
Observation: 笔记已保存
```

### Implementation

新增:

```text
agentic_core/trace_view.py
```

提供:

```python
format_trace_brief(trace: list[dict]) -> str
```

CLI 支持:

```bash
AGENTIC_TRACE=brief python3 -m agentic_core.cli "..."
AGENTIC_TRACE=json python3 -m agentic_core.cli "..."
```

### Acceptance Criteria

- 默认 CLI 可以继续输出完整 JSON。
- 设置 `AGENTIC_TRACE=brief` 时输出简洁 trace。
- chat 模式默认使用简洁 trace 或只输出最终答案。

## Phase 6: 记录 Hermes 原始输出

### Goal

为了学习“模型为什么不可靠”,需要能看到 Hermes 原始输出和 fallback 原因。

当前 trace 只看到:

```text
Hermes planner fallback: calculator requires input.expression
```

下一步可以保存:

```python
raw_model_output
parse_error
fallback_action
```

### Implementation

更新 `HermesPlanner.next()`:

- 成功时可记录 raw content。
- 失败时把 raw content 和 error 放到 action metadata。

可扩展 `Action`:

```python
metadata: dict[str, Any] = field(default_factory=dict)
```

### Acceptance Criteria

当 Hermes 输出非法 JSON 或缺少参数时:

- trace 中能看到模型原始输出。
- trace 中能看到失败原因。
- 系统仍然 fallback 并完成任务。

## Recommended Implementation Order

建议按以下顺序做,每一步都能独立运行:

1. `chat.py` 连续对话模式
2. `JsonMemoryStore` 持久化到 `agentic/data/memory.json`
3. Planner 读取长期记忆并影响学习计划
4. `study.plan` 工具
5. `trace_view.py` 简洁 trace
6. Hermes 原始输出记录

不要一口气全部改完。每一步都应该:

```text
小改动 -> 跑命令 -> 看 trace -> 确认理解 -> 再下一步
```

## Test Plan

### Basic Regression

```bash
cd /Users/jurongchao/Desktop/ai学习测试库/agentic
python3 -m compileall agentic_core examples
AGENTIC_PLANNER=rule python3 -m agentic_core.cli "帮我计算 128 * 7, 然后记录成学习笔记"
python3 examples/run_memory_demo.py
```

### Chat Mode

```bash
python3 -m agentic_core.chat
```

输入:

```text
以后安排学习任务时，每次控制在30分钟以内
帮我安排 agentic 学习计划
exit
```

期望第二轮能读取第一轮的偏好。

### Persistence

```bash
python3 -m agentic_core.chat
```

保存一条长期记忆后退出,检查:

```text
agentic/data/memory.json
```

再次启动 chat,确认记忆仍存在。

### Planner Uses Memory

保存:

```text
以后安排学习任务时，每次控制在30分钟以内
```

再输入:

```text
帮我安排 agentic memory 的学习计划
```

期望回答或工具输出体现 30 分钟限制。

## Notes

这几个改进背后的核心学习目标是:

```text
从“单次 agent loop”
升级到
“多轮对话 + 可持久记忆 + 记忆影响规划”
```

这也是从 demo 走向真正 agentic 应用的关键一步。
