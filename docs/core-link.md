# Agentic 应用核心链路设计

## 目标

一个 agentic 应用不是简单的“问一次模型,回一次答案”,而是让模型在受控环境里完成:

- 理解目标
- 拆解任务
- 选择工具
- 执行工具
- 观察结果
- 调整计划
- 给出答案
- 沉淀记忆

## 核心模块

### 1. Agent Orchestrator

负责控制主循环:

```text
while not done:
  context = buildContext(goal, memory, trace, tools)
  action = planner.next(context)
  observation = executor.run(action)
  memory.write(observation)
  trace.append(action, observation)
```

关键设计点:

- 必须有最大步数,避免无限循环。
- 每一步都要记录 trace。
- 工具失败不应该直接崩溃整个 agent,而应变成 observation。

### 2. Planner

Planner 是“下一步怎么做”的决策器。真实项目里通常由 LLM 实现,但它应该输出结构化动作:

```json
{
  "type": "tool",
  "toolName": "calculator",
  "input": { "expression": "128 * 7" },
  "reason": "需要先得到计算结果"
}
```

或:

```json
{
  "type": "final",
  "answer": "已经完成..."
}
```

### 3. Tool Registry

工具必须显式注册:

- name
- description
- schema 或输入约束
- execute 函数

这能让 agentic 应用保持边界清晰:模型只负责选择,系统负责校验和执行。

### 4. Memory

最小可用记忆分两类:

- short-term: 当前 run 的 observations 和 trace。
- long-term: 跨 run 保存的用户偏好、笔记、任务状态。

当前 demo 使用内存实现。生产环境建议用数据库并加上 TTL、标签、向量检索和权限隔离。

### 5. Trace

Trace 是 agentic 应用能不能调试的关键。每一步至少记录:

- step
- action
- observation
- elapsedMs
- error

没有 trace 的 agent 很难定位“为什么做了这个决定”。

## 生产化关注点

- **权限控制**: 高风险工具需要审批,例如发邮件、支付、删除数据。
- **幂等性**: 外部副作用工具要支持 request id。
- **超时和重试**: 工具执行必须有超时,重试要有上限。
- **结果校验**: 对工具返回做 schema 校验。
- **成本控制**: 限制最大轮数、最大 token、最大工具调用次数。
- **评估闭环**: 对最终答案和中间步骤进行自动或人工评估。
