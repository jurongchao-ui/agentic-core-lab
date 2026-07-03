# Agentic Core Operations

## 环境变量

组件切换:

```text
AGENTIC_MODEL=openhermes:latest
AGENTIC_PLANNER=hermes|rule
AGENTIC_MEMORY_POLICY=llm|rule
AGENTIC_SAFETY_POLICY=rule|llm|composite
AGENTIC_SAFETY_FAIL_CLOSED=1|0
```

身份上下文:

```text
AGENTIC_USER_ID=local_user
AGENTIC_TENANT_ID=default_tenant
AGENTIC_ROLES=developer,admin
AGENTIC_PERMISSION_SCOPES=tool:calculator:read,memory:note:write
```

Trace:

```text
AGENTIC_TRACE=off|brief|json
AGENTIC_CHAT_DEBUG=1
```

记忆:

```text
AGENTIC_MEMORY_STORE=memory|json
AGENTIC_MEMORY_PATH=data/memory.json
```

事件日志:

```text
AGENTIC_EVENT_LOG=memory|jsonl|sqlite
AGENTIC_EVENT_LOG_PATH=data/events.jsonl  # sqlite 时通常是 data/events.db
AGENTIC_EVENT_LOG_MAX_BYTES=10485760
AGENTIC_EVENT_LOG_BACKUP_COUNT=3
AGENTIC_EVENT_LOG_LOCK=1|0
```

Chat 输入:

```text
AGENTIC_CHAT_PROMPT=User>
AGENTIC_CHAT_INLINE_PROMPT=1
```

## 运行模式

完全离线:

```bash
AGENTIC_PLANNER=rule AGENTIC_MEMORY_POLICY=rule AGENTIC_SAFETY_POLICY=rule \
  python3 -m agentic_core.cli "帮我计算 128 * 7"
```

Hermes planner + LLM memory:

```bash
AGENTIC_MODEL=openhermes:latest python3 -m agentic_core.cli "帮我计算 128 * 7"
```

持久化记忆 + 事件日志:

```bash
AGENTIC_MEMORY_STORE=json AGENTIC_EVENT_LOG=jsonl \
  python3 -m agentic_core.chat
```

SQLite 事件日志:

```bash
AGENTIC_EVENT_LOG=sqlite AGENTIC_EVENT_LOG_PATH=data/events.db \
  python3 -m agentic_core.chat
```

限制当前身份只能调用 calculator:

```bash
AGENTIC_PLANNER=rule AGENTIC_MEMORY_POLICY=rule \
AGENTIC_PERMISSION_SCOPES=tool:calculator:read \
  python3 -m agentic_core.cli "帮我计算 128 * 7"
```

如果同一身份再尝试写笔记,`memory:note:write` 会被 ToolGovernanceMiddleware 拦截。

## 可观测性

Brief trace:

```bash
AGENTIC_TRACE=brief python3 -m agentic_core.cli "帮我计算 128 * 7"
```

完整 JSON:

```bash
AGENTIC_TRACE=json python3 -m agentic_core.cli "帮我计算 128 * 7"
```

查看事件日志:

```bash
python3 -m agentic_core.event_log --path data/events.jsonl
python3 -m agentic_core.event_log --path data/events.jsonl --run-id run_123
python3 -m agentic_core.event_log --path data/events.jsonl --current-only
python3 -m agentic_core.event_log --backend sqlite --path data/events.db
```

## 安全与敏感信息

请求级 global safety 在最前面运行。命中后:

- 不评估记忆。
- 不调用 planner。
- 不执行工具。
- 直接返回拒绝回复。

敏感信息 local safety:

- 长期记忆不保存密码、密钥、证件号等。
- `note.add`、`todo.add`、`memory.add` 复用同一份 `SENSITIVE_PATTERN`。
- 事件日志写入前会脱敏。

## 运行时产物

以下产物不应提交:

```text
data/memory.json
data/*.jsonl
data/*.jsonl.lock
```

它们已经由 `.gitignore` 管理。

## 测试门禁

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m mypy agentic_core
python3 -m compileall agentic_core examples tests
python3 -m agentic_core.eval_harness
```

CI 使用 `.github/workflows/ci.yml` 跑 mypy + pytest。
