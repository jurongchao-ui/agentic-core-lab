# Agentic Core Evals

## 目标

Eval harness 用来把 agentic 行为变成可回归的质量门禁。

它不是只看最终答案,还检查:

- 工具是否被正确调用。
- 工具失败是否被正确处理。
- 记忆是否真实保存。
- ResponsePolicy 是否走到正确档位。
- SafetyPolicy 是否拦截。
- 事件是否按预期 emit。
- 汇总指标是否超过阈值。

## 运行

文本报告:

```bash
python3 -m agentic_core.eval_harness
```

JSON 报告:

```bash
python3 -m agentic_core.eval_harness --json
```

用例失败或 gate 失败时退出码非 0,适合接 CI。

## 默认用例

当前默认 8 个确定性用例:

```text
calculator_note
memory_preference_save
study_plan_uses_memory
safety_refusal
sensitive_memory_rejected
tech_stack_clarification
tech_stack_save
failed_calculation_blocks_note
```

覆盖点:

- 计算 + 记笔记。
- 长期偏好保存。
- 长期记忆影响学习计划。
- global safety 拒绝。
- 敏感记忆拒绝。
- 技术栈缺信息追问。
- 技术栈保存。
- 计算失败不写笔记。

## 报告结构

`EvalReport` 包含:

```text
total
passed
failed
passedGate
metrics
eventCounts
thresholds
gateFailures
cases
```

每个 `EvalCaseResult` 包含:

```text
name
passed
failures
answer
status
tool_names
response_tiers
memory_texts
event_counts
metrics
```

## 汇总指标

```text
case_pass_rate
tool_calls
tool_failures
tool_success_rate
planner_fallbacks
safety_refusals
memory_saved
memory_decisions
run_failed
avg_steps
```

## 默认阈值

`EvalThresholds`:

```text
minCasePassRate = 1.0
minToolSuccessRate = 0.75
maxRunFailed = 0
maxPlannerFallbacks = 0
```

`failed_calculation_blocks_note` 会产生一次预期工具失败,所以默认工具成功率阈值不是 1.0。

## 扩展方式

新增用例时优先断言稳定结构:

- `expected_status`
- `expected_tools`
- `expected_response_tiers`
- `expected_event_counts`
- `expected_memory_saves`
- `expected_tool_failures`
- `expected_memory_contains`

少绑定完整自然语言回复,多绑定关键片段和结构化事件。

