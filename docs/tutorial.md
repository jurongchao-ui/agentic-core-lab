# Agentic Core Lab Tutorial

## 目标

这份教程用于从零跑通 Python 主线:

```text
User Input
  -> SafetyPolicy
  -> MemoryPolicy
  -> Planner
  -> MiddlewarePipeline
  -> ToolRegistry
  -> Observation
  -> ResponsePolicy
  -> Final Answer
```

JS v0 只作为早期参考,当前学习主线是 `agentic_core/`。

## 准备

确保在项目目录:

```bash
cd /Users/jurongchao/Desktop/ai学习测试库/agentic
```

如果要使用 Hermes/Ollama:

```bash
ollama list
```

完全离线学习可使用规则模式:

```bash
AGENTIC_PLANNER=rule AGENTIC_MEMORY_POLICY=rule python3 -m agentic_core.cli "帮我计算 128 * 7"
```

## 单次运行

CLI 是“一句话运行一次”:

```bash
python3 -m agentic_core.cli "以后安排学习任务时，每次控制在30分钟以内"
python3 -m agentic_core.cli "帮我计算 128 * 7, 然后记录成学习笔记"
```

默认 CLI 会打印完整 JSON trace。可以切换:

```bash
AGENTIC_TRACE=brief python3 -m agentic_core.cli "帮我计算 128 * 7"
AGENTIC_TRACE=off python3 -m agentic_core.cli "帮我计算 128 * 7"
```

## 连续对话

Chat 入口会启动一个长期运行的 Python 进程,多轮输入共用同一个 `MemoryStore`:

```bash
python3 -m agentic_core.chat
```

退出命令:

```text
exit
quit
退出
```

如果终端中文输入/删除显示不稳定,默认两行输入模式会减少残留。也可以自定义:

```bash
AGENTIC_CHAT_PROMPT='You> ' python3 -m agentic_core.chat
AGENTIC_CHAT_INLINE_PROMPT=1 python3 -m agentic_core.chat
```

## 记忆持久化

默认记忆只在当前进程内。跨 CLI/chat 保留 notes、todos、longTermMemories:

```bash
AGENTIC_MEMORY_STORE=json python3 -m agentic_core.cli "以后安排学习任务时，每次控制在30分钟以内"
AGENTIC_MEMORY_STORE=json python3 -m agentic_core.chat
```

默认文件:

```text
data/memory.json
```

自定义路径:

```bash
AGENTIC_MEMORY_STORE=json AGENTIC_MEMORY_PATH=data/my-memory.json python3 -m agentic_core.chat
```

## 记忆影响规划

先保存学习时长偏好:

```bash
AGENTIC_PLANNER=rule AGENTIC_MEMORY_POLICY=rule AGENTIC_MEMORY_STORE=json \
  python3 -m agentic_core.cli "以后安排学习任务时，每次控制在30分钟以内"
```

再安排学习计划:

```bash
AGENTIC_PLANNER=rule AGENTIC_MEMORY_POLICY=rule AGENTIC_MEMORY_STORE=json \
  python3 -m agentic_core.cli "帮我安排 agentic memory 的学习计划"
```

第二条会读取第一条长期偏好,调用 `study.plan`,生成不超过 30 分钟的学习计划。

## 事件日志

开启 JSONL 事件日志:

```bash
AGENTIC_EVENT_LOG=jsonl python3 -m agentic_core.cli "帮我计算 128 * 7"
AGENTIC_EVENT_LOG=jsonl AGENTIC_EVENT_LOG_PATH=data/events.jsonl python3 -m agentic_core.chat
```

查看事件:

```bash
python3 -m agentic_core.event_log --path data/events.jsonl
python3 -m agentic_core.event_log --path data/events.jsonl --run-id run_123
python3 -m agentic_core.event_log --path data/events.jsonl --current-only
```

## Eval

运行确定性 eval:

```bash
python3 -m agentic_core.eval_harness
python3 -m agentic_core.eval_harness --json
```

当前 eval 完全离线,使用规则 planner/policy/safety,适合做稳定回归门禁。

