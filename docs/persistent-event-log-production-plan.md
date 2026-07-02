# Persistent Event Log 生产级修订方案

## Summary

本方案修订 Persistent Event Log 的实施路线：不再先把 JSONL 写入逻辑硬编码到 `MemoryStore`，而是先建立 `EventWriter` 抽象，再把 JSONL 作为可替换后端接入。

这样做符合当前项目已经建立的 Typed State / Protocol / DI 方向，也避免“先焊死、再拆开”的返工。

本方案只描述设计和后续实施路线，不实现代码，不创建 `data/events.jsonl`。

## 当前状态

项目已经具备事件日志的结构基础：

- `EventRecord`: 单条事件结构。
- `TraceStep`: 单次 run 内的 step 级执行轨迹。
- `AgentRunResult`: 单次 run 的内存聚合结果。
- `MemorySnapshot`: 某一刻的记忆快照。
- `MemoryStore.events`: 当前进程内事件列表。

当前限制：

- `events` 只存在内存里，进程退出后消失。
- `record_event()` 仍保留旧字典兼容入口。
- 事件 payload 还没有统一 source、level、schemaVersion、redacted 等生产字段。
- `data/events.jsonl` 尚未实现，也不应该在第一步直接焊进 `MemoryStore`。

## 分工边界

Persistent Event Log 不能重复造一套和 Typed State 平行的数据结构。它应该复用现有 typed state。

建议分工：

- `AgentRunResult`: 单次 run 的内存聚合结果，用于 CLI/Chat 返回和当前轮展示。
- `TraceStep`: 单次 run 内的 Plan-Act-Observe step 轨迹。
- `EventRecord`: 可持久化、可检索、可审计的事件单位。
- `Event Log`: 跨 run 的 append-only 事件流。
- `trace_view.py`: 人读展示层，不负责持久化。

一句话：

```text
AgentRunResult 是一次运行的结果快照。
Event Log 是很多次运行的事件流。
```

## 为什么先做 EventWriter

原路线是：

```text
阶段 1: MemoryStore 直接写 JSONL
阶段 2: 再抽 EventWriter
```

这个顺序会导致返工。因为 JSONL 写入逻辑一旦进入 `MemoryStore`，后面为了支持 SQLite/Postgres/OTel 又要把它拆出来。

修订后的路线是：

```text
阶段 1: EventWriter 抽象 + MemoryEventWriter
阶段 2: JsonlEventWriter + CompositeEventWriter
```

好处：

- 默认行为可以保持纯内存，零行为变化。
- JSONL 只是 drop-in writer。
- 测试可以注入 fake writer。
- 未来数据库/可观测平台也只是新增 writer。
- 符合当前 Protocol + DI 的项目方向。

## EventWriter 抽象

建议新增协议：

```python
class EventWriter(Protocol):
    def write(self, event: EventRecord) -> None:
        ...
```

第一批实现：

- `MemoryEventWriter`: 写入当前进程内存，保持现有学习版行为。
- `JsonlEventWriter`: 追加写入 `data/events.jsonl`。
- `CompositeEventWriter`: 同时写多个 writer，例如内存 + JSONL。

未来扩展：

- `SQLiteEventWriter`
- `PostgresEventWriter`
- `ClickHouseEventWriter`
- `S3EventWriter`
- `OtelEventWriter`

## Call Site 迁移前置

当前 `record_event()` 支持旧字典形态：

```python
record_event({"runId": "...", "type": "...", "decision": ...})
```

为了生产级事件结构干净，后续新代码应统一使用显式形态：

```python
record_event(
    event_type="memory_decision",
    run_id=state.run_id,
    payload={...},
)
```

迁移原则：

- 旧字典形态只保留短期兼容。
- 新 call site 一律使用 `event_type/run_id/payload`。
- payload 内部结构按事件类型稳定下来。
- 不让 JSONL 里继续出现参差不齐的历史格式。

这一步是落地 `source`、`level`、`schemaVersion`、`redacted` 等字段的前置。

## Agent 生命周期事件

如果验收要求“按 `runId` 找回完整事件链路”，就必须由 Agent 层 emit 生命周期事件。

建议事件边界：

- Agent 层 emit:
  - `run_started`
  - `safety_decision`
  - `planner_action`
  - `tool_started`
  - `tool_observation`
  - `response_decision`
  - `run_completed`
  - `run_failed`

- MemoryPolicy / MemoryStore 相关:
  - `memory_decision`
  - `memory_saved`
  - `memory_clarification`

- SafetyPolicy 相关:
  - `safety_decision`
  - `safety_refusal`

当前代码已有 `run_started`、`memory_decision`、`memory_saved`、`tool_observation`、`response_decision`、`run_completed` 等基础事件，但仍需要统一 payload 和补齐缺失事件。

## 事件字段原则

生产级事件建议包含：

```text
id
runId
type
createdAt
schemaVersion
payload
level
source
redacted
```

字段说明：

- `id`: 事件唯一 ID。
- `runId`: 一次 Agent run 的关联 ID。
- `type`: 稳定事件类型，程序读取它，不读取文案。
- `createdAt`: 事件发生时间。
- `schemaVersion`: 事件 schema 版本，方便未来迁移。
- `payload`: 事件载荷。
- `level`: `info` / `warn` / `error`。
- `source`: `agent` / `planner` / `tool` / `memory` / `safety` / `response`。
- `redacted`: 是否经过脱敏。

注意：这些字段应该优先进入 `EventRecord` / writer 序列化层，而不是在各个 call site 手写。

## 事件类型

建议稳定事件类型：

- `run_started`
- `safety_decision`
- `safety_refusal`
- `memory_decision`
- `memory_saved`
- `memory_clarification`
- `planner_action`
- `planner_fallback`
- `tool_started`
- `tool_observation`
- `response_decision`
- `run_completed`
- `run_failed`

事件类型应该作为 API 契约对待。后续如果改名，要考虑兼容旧 event log。

## JSONL 后端

JSONL 是第一版持久化后端，但不是第一步的抽象边界。

默认文件：

```text
data/events.jsonl
```

格式：

```text
一行 = 一个事件 JSON
```

示例：

```json
{"type":"run_started","runId":"run_1","createdAt":"2026-07-02T00:00:00+00:00","payload":{"goal":"帮我计算 128 * 7"}}
{"type":"tool_observation","runId":"run_1","createdAt":"2026-07-02T00:00:01+00:00","payload":{"toolName":"calculator","ok":true}}
{"type":"run_completed","runId":"run_1","createdAt":"2026-07-02T00:00:02+00:00","payload":{"status":"completed"}}
```

JSONL 优点：

- append-only，实现简单。
- 一行一条事件，适合日志流。
- 程序崩溃时，不容易损坏整份日志。
- 可用 `rg` / `jq` / Python 标准库快速分析。
- 不需要数据库，适合当前学习阶段。

运行时产物必须加入 `.gitignore`：

```text
data/events.jsonl
```

如果后续产生更多运行时数据，也建议忽略：

```text
data/*.jsonl
```

## 脱敏策略

事件日志不要维护第二套敏感规则。

项目已有敏感信息真相源：

```python
memory_policy.SENSITIVE_PATTERN
```

后续 redaction 应复用并扩展这一份 pattern，而不是在 Event Log 里再写一套规则。

需要覆盖：

- 密码
- 密钥
- API key
- token
- cookie
- 私钥
- 访问凭证
- 身份证号
- 银行卡号
- 验证码

建议：

- `SENSITIVE_PATTERN` 扩展后由 MemoryPolicy、Tool guard、Event redaction 共用。
- 工具输入进入事件前先 redaction。
- LLM 原始输出进入 metadata 前先截断或 redaction。
- 事件中设置 `redacted=true` 表示已处理。

## 可靠性原则

Persistent Event Log 是可观测基础设施，不能拖垮用户主流程。

原则：

- writer 写入失败不能让 Agent run 失败。
- JSONL 写入失败时，内存事件仍保留。
- 写入失败应产生 warning 事件或测试可观测信号。
- 单条事件必须可 JSON 序列化。
- 不修改历史事件，修正信息通过新事件追加。
- payload 不保存未脱敏的敏感信息。

推荐流程：

```text
emit event
  -> MemoryEventWriter
  -> JsonlEventWriter
       success: ok
       fail: warning + continue
```

## Timeline Reconstruction，不是 Deterministic Replay

阶段 3 不应叫“确定性 replay”。

应该叫：

```text
Event Timeline / Run Inspector
```

它的目标是：

- 按 `runId` 查询事件。
- 按时间顺序打印事件链路。
- 辅助 debug、排障、人工复盘。

它不承诺重新执行并得到同样结果。

真正 deterministic replay 需要固化：

- LLM 原始输出。
- 工具输出。
- 时间戳。
- 随机数。
- 外部 API 响应。
- 环境变量和配置。

当前项目已有 `rawModelOutput` metadata，这为未来 replay 埋了伏笔，但还不足以承诺确定性重放。

## 修订后的实施路线

### 阶段 1: EventWriter 抽象与默认内存实现

目标：

- 新增 `EventWriter` Protocol。
- 新增 `MemoryEventWriter`。
- `MemoryStore` 通过 DI 使用 writer。
- 默认行为保持当前纯内存事件。
- 新 call site 统一使用 `event_type/run_id/payload`。

验收：

- CLI/Chat 行为不变。
- `MemoryStore.events` 仍正常工作。
- 测试可以注入 fake writer。
- 不创建 `data/events.jsonl`。

### 阶段 2: JSONL 持久化后端

目标：

- 新增 `JsonlEventWriter`。
- 新增 `CompositeEventWriter`。
- 默认路径为 `data/events.jsonl`。
- 将 `data/events.jsonl` 或 `data/*.jsonl` 加入 `.gitignore`。
- 写入失败不影响 Agent 主流程。

验收：

- 每次运行后 JSONL 增加事件行。
- 每行都是合法 JSON。
- 内存 events 和 JSONL events 都存在。
- 敏感字段已脱敏。

### 阶段 3: Agent 生命周期事件补齐

目标：

- 补齐 `run_failed`、`safety_decision`、`planner_action`、`tool_started` 等事件。
- 明确事件由哪一层 emit。
- 统一 payload 结构。

验收：

- 按 `runId` 可以找到完整事件链路。
- 成功、拒绝、追问、工具失败、max_steps 等路径都有结束事件。

### 阶段 4: Event Timeline / Run Inspector

目标：

- 新增按 `runId` 读取 JSONL 的工具。
- 输出某次运行的事件时间线。
- 用于 debug、排障、人工复盘。

验收：

```text
run_started
safety_decision
memory_decision
planner_action
tool_started
tool_observation
response_decision
run_completed
```

### 阶段 5: Eval Harness

目标：

- 基于事件日志统计行为指标。
- 对比不同版本 Agent 的行为变化。

当前已落地学习版 `agentic_core.eval_harness`：

- 使用规则 planner / memory policy，保证不依赖 Ollama。
- 内置确定性用例：计算+笔记、长期记忆保存、记忆影响学习计划、安全拒绝、敏感记忆拒绝。
- 输出文本或 JSON 报告。
- 统计单次 run 的事件指标，并汇总全套 eval 指标。

可统计：

- tool 调用成功率。
- planner fallback 次数。
- safety refusal 次数。
- memory save/reject 分布。
- 平均 step 数。
- run_failed 比例。

### 阶段 6: 生产后端与治理

JSONL 是学习版后端。生产环境还要考虑：

- 文件增长。
- rotation（当前 JSONL 已有按大小轮转的学习版实现）。
- retention（当前 JSONL 已有 backup_count 备份保留）。
- 多进程并发追加（当前 JSONL 已有基础文件锁,保护单机多进程的轮转和追加写入）。
- 文件锁跨平台差异（macOS/Linux 使用 `fcntl.flock`;不支持的平台会降级为无锁写入）。
- 数据库后端。
- 可观测平台接入。

演进后端：

- SQLite: 本地结构化查询。
- Postgres: 业务系统可查询。
- ClickHouse: 大规模事件分析。
- S3/Object Storage: 低成本长期归档。
- OpenTelemetry: 接入生产可观测平台。

## 后续测试建议

实现时建议补充：

- `MemoryEventWriter` 写入内存。
- `JsonlEventWriter` 每行输出合法 JSON。
- `CompositeEventWriter` 同时调用多个 writer。
- writer 写入失败不影响 Agent 主流程。
- `data/events.jsonl` 被 `.gitignore` 忽略。
- payload redaction 复用 `SENSITIVE_PATTERN`。
- 按 `runId` 能查询完整 timeline。
- `pytest` / `mypy` / `compileall` 通过。

## 本阶段边界

这份文档只是修订方案。

不做：

- 不实现 `EventWriter`。
- 不创建 `data/events.jsonl`。
- 不修改 `MemoryStore`。
- 不修改 `Agent`。
- 不修改 `.gitignore`。

真正实现时，第一步应从 `EventWriter` 抽象开始，而不是直接写 JSONL。
