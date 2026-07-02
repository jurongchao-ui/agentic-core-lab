---
title: Agentic 回复策略设计
type: learning_note
topic: agentic response policy
created: 2026-07-02
source: codex_session
tags: [agentic, response-policy, codex, memory]
---

# Agentic 回复策略设计

## 1. 为什么需要 ResponsePolicy

在 agentic 应用里，最终回复不能简单交给一个普通的 LLM responder。

原因是 agent 在回复用户之前，可能已经做过很多重要动作：

- 判断是否需要保存长期记忆
- 保存了用户偏好或用户资料
- 调用了工具
- 得到了工具 observation
- 发现信息不足，需要追问
- 发现敏感信息，需要拒绝保存或提醒

如果最后直接让 responder 重新生成一句话，它可能会覆盖这些关键结果。

比如：

```text
用户：我的技术栈是 Node.js 和 React，Codex
MemoryPolicy：判断应该保存
MemoryStore：已经写入 long_term_memories
Responder：返回一句普通闲聊
```

这时系统内部状态是对的，但用户看到的回复是不合理的。

所以要引入 `ResponsePolicy`：

```text
Responder 负责自然语言表达。
ResponsePolicy 负责最终回复仲裁。
```

## 2. Codex 回复策略的可借鉴思想

Codex 的内部实现细节不是公开接口的一部分，不能假设它的源码就是某种结构。

但从公开行为和官方手册可以抽象出一种常见模式：

```text
指令层级
-> 当前模式
-> 用户最新请求
-> 上下文和项目规则
-> 工具执行结果
-> 最终面向用户的表达
```

也就是说，Codex 不是简单的：

```text
用户说一句 -> 模型直接回一句
```

而更像：

```text
任务分类
-> 上下文整理
-> 工具调用
-> 状态观察
-> 回复策略选择
-> 最终回复
```

这对当前 `agentic_core` 的启发是：

```text
Agent.run()
  -> MemoryPolicy
  -> Planner
  -> ToolExecutor
  -> Observation
  -> ResponsePolicy
  -> Final Answer
```

其中 `LlmResponder` 只是普通对话时的兜底表达层，不应该掌握最终回复权。

## 3. 推荐的回复优先级

把最终回复做成优先级策略，但要分成三类，不能一律"择一即停"：

```text
拦截档(命中即停,不再往下):
  1. clarification         信息不足 -> 追问
  2. global safety         危险/越权操作 -> 拒绝并说明

内容档(可同时命中 -> 拼接输出):
  3. local safety          局部敏感项 -> 拒绝该项,不代表整轮失败
  4. memory confirmation   已保存 -> 确认
  5. tool result summary   调过工具 -> 汇总结果
  6. failure / incomplete  有失败或没跑完 -> 据实说明

兜底档(内容档一个都没命中才用):
  7. planner answer        LLM planner 直接给的 final.answer
  8. normal responder      普通聊天/解释
```

为什么要分组：

一句话可以同时触发多个"内容档"。例如"帮我算 128×7，并记住我以后每次只学 30 分钟"——
既有工具结果、又存了记忆。如果严格择一，就会丢掉另一半，而且排序本身也可疑
(用户主要想看的是算出 896，记忆确认是次要的)。所以：

- **拦截档**：命中一个就停(该追问/该全局拒绝时，不应再做别的)。
- **内容档**：全部命中的都要拼进最终回复。
- **兜底档**：只有内容档一个都没命中，才交给 planner / responder。

当前学习版采用一个保守取舍：

```text
safety:
  敏感记忆保存 = 局部拒绝
  危险工具/越权操作 = 全局拒绝

clarification:
  当前阶段 = 全局追问
  后续有 intent splitter 后,再支持"能做的先做,缺的信息再问"
```

含义如下。

### 3.1 clarification

如果信息不足，优先追问，不进入 planner，也不让 responder 编造。

当前阶段 clarification 使用全局拦截：

```text
只要 MemoryPolicy.needs_clarification=True
就直接追问,整轮不进入 planner
```

原因是当前链路还是：

```text
整句 goal -> MemoryPolicy -> Planner
```

而不是：

```text
goal -> intent splitter -> memory intent + tool intent -> 分别处理
```

所以如果现在强行做局部追问，会让最小实现过早复杂化。后续引入 intent splitter 后，再支持“能做的先做，缺的信息再问”。

例子：

```text
用户：请把我的技术栈计入长期记忆里
系统：可以，请告诉我你的技术栈具体包括哪些？
```

### 3.2 safety

safety 要区分全局拦截和局部拦截。

全局拦截用于危险操作、越权操作、会造成明显风险的请求。命中后整轮停止。

局部拦截用于某个子任务不合适，但其它安全任务理论上仍可以继续。例如“保存密码”不应该做，但“计算 128 * 7”本身是安全任务。

当前最小实现还没有成熟的多意图拆分能力，所以第一版可以先做到：

```text
敏感记忆输入不保存,并在最终回复中说明。
未来增强后,支持"敏感记忆拒绝 + 安全子任务继续执行"。
```

例子：

```text
用户：请记住我的密码是 123456
系统：这类信息不适合进入长期记忆，我不会保存。
```

未来增强后的组合例子：

```text
用户：请记住我的密码是 123456，然后帮我计算 128 * 7
系统：这类信息不适合进入长期记忆，我不会保存。计算结果是 896。
```

### 3.3 memory confirmation

如果系统已经保存了长期记忆，最终回复应该确认保存结果。

例子：

```text
用户：请记住我的技术栈是 Python、FastAPI、React
系统：已记住：你的技术栈是 Python、FastAPI、React。
```

这里的关键点是：

```text
保存动作已经发生 -> 回复必须尊重这个事实
```

### 3.4 tool result summary

如果调用了工具，优先总结工具结果。

例子：

```text
用户：帮我计算 128 * 7，然后记录成学习笔记
系统：计算结果是 896，并已记录到学习笔记。
```

不能让 responder 覆盖工具 observation。

### 3.5 planner answer

如果 planner 已经给出明确答案，并且没有更高优先级事件，就使用 planner 的答案。

### 3.6 normal responder

只有普通聊天、解释、学习类问题，才交给 `LlmResponder`。

例子：

```text
用户：Hermes 有什么用？
系统：由 responder 生成自然解释。
```

### 3.7 failure / incomplete

如果有工具失败、或循环到 `max_steps` 仍未完成，要据实说明，不能掩盖，也不能让 responder 编一个"已完成"。

例子：

```text
用户：帮我算 128 / 0，然后记成笔记
系统：计算失败(除数不能为 0)，因此没有生成笔记。
```

这一档和 3.3 / 3.4 一样属于内容档，和成功的结果一起拼接。当前 `Agent.run` 已经有"达到最大步数"和失败 observation 的分支，ResponsePolicy 要接住它们，而不是留白或谎报。

### 3.8 内容拼接规则

内容档可以同时命中，但拼接时必须遵守几个硬规则：

```text
只确认已经真实发生的事。
只总结 observation 里真实成功的结果。
失败会阻断依赖它的后续确认。
不要根据用户原始意图编造完成状态。
```

例如：

```text
用户：帮我算 128 / 0，然后记成笔记
```

如果 calculator 失败，就不能回复“已记录笔记”。只能回复：

```text
计算失败：除数不能为 0，因此没有记录学习笔记。
```

再例如：

```text
用户：帮我计算 128 * 7，然后记录成学习笔记
```

只有当 trace 里真实出现：

```text
calculator ok -> 896
note.add ok
```

才可以回复：

```text
计算结果是 896，并已记录到学习笔记。
```

推荐拼接顺序：

```text
1. local safety notice
2. memory confirmation
3. tool success summary
4. failure / incomplete
5. next question / next step
```

如果同时有成功和失败，回复要区分“已完成的事实”和“未完成的部分”：

```text
已记住：每次学习控制在 30 分钟以内。计算失败：除数不能为 0，因此没有记录学习笔记。
```

## 4. ResponsePolicy 和 Responder 的职责拆分

推荐职责如下：

```text
ResponsePolicy
负责判断最终应该回复什么类型。

Responder
负责在普通对话场景下生成自然语言。
```

不要让 responder 处理这些事：

- 是否追问
- 是否拒绝保存敏感信息
- 是否确认长期记忆已保存
- 是否总结工具结果
- 是否覆盖 planner 的动作结果

这些属于系统策略，不属于自由生成。

## 5. 在当前项目中的落地结构

当前 `agentic_core` 可以新增一个模块：

```text
agentic_core/response_policy.py
```

建议接口（返回一个可审计的决策对象，而不是光秃秃的字符串）：

```python
@dataclass
class ResponseContext:
    goal: str
    memory_decision: MemoryDecision
    saved_memories: list[dict[str, Any]]
    trace: list[dict[str, Any]]
    planner_answer: str | None
    incomplete_reason: str | None
    memory_snapshot: dict[str, Any]
    responder: Any | None = None

@dataclass
class ResponseDecision:
    text: str          # 最终面向用户的话
    tiers: list[str]   # 命中了哪些档(便于 trace 观测)
    reason: str        # 为什么这么回

class ResponsePolicy:
    def decide(self, context: ResponseContext) -> ResponseDecision:
        ...
```

四个要点：

- 返回 `tiers` 和 `reason`，是为了让 debug trace 能看出"最终回复为什么这么生成"(见 §8 可观测性)。
- 输入用 `ResponseContext`，避免 `decide()` 的参数越来越长；以后加字段也不破坏函数签名。
- `saved_memories` 必须是 list，而不是单个 `saved_memory`。因为未来可能出现 MemoryPolicy 自动保存、`memory.add` 工具保存、一次输入抽取多条长期记忆等情况。
- "tool result summary"这一档**不要重写**：直接复用 planner 里已有的 `build_answer`(它已经在汇总 calculator / note / todo)。更进一步，应该把 `build_answer` 从 planner **搬到** ResponsePolicy——因为本设计的隐含前提是：**planner 只决定动作，不再产出面向用户的话**。
- 因此 `planner_answer` 只在"LLM planner 直接给了 final.answer"时才有值，对应兜底档 6。

调用位置放在 `Agent.run()` 的最后阶段。

原来容易出问题的结构是：

```text
没有工具 trace -> responder.reply()
有工具 trace -> action.answer
```

更合理的结构是：

```text
无论有没有工具 trace
都先交给 ResponsePolicy 判断最终回复
```

## 6. 建议实现顺序

第一阶段：手写规则版 `ResponsePolicy`

- 不引入新依赖
- 用 if / elif 明确表达优先级
- 让初学者能看懂每一步为什么这么判断

第二阶段（可选，需单独权衡）：引入 Pydantic

注意这**不是"顺手迁移"**。当前项目刻意零依赖、用 stdlib dataclass(见 `pyproject.toml` 的 `dependencies=[]`)。

- 收益：把手写的 `coerce_confidence`、`validate_tool_input` 这类校验交给 schema。
- 代价：新依赖 + 改动面不小(所有 `to_dict`/`asdict`、trace 渲染都要跟着改)。

只有当目标明确是"更稳的 LLM 输入输出"时才做，别当成既定的下一步。

第三阶段（可选，且和当前定位冲突）：迁移到 LangGraph

把链路变成状态图确实优雅：

```text
memory_policy_node -> planner_node -> tool_node -> response_policy_node
```

但 LangGraph 会引入重依赖，并把这个 lab 最有价值的东西——"亲手看懂核心循环"——藏进框架里。
只有当目标从"学内部原理"转向"上生产 agent"时才考虑；当前阶段**不建议**。

## 7. 非目标与边界

这一层是"回复仲裁"，不负责下面这些(避免读者误以为顺带解决)：

- **记忆去重**：agent 层保存 + `memory.add` 工具保存导致的重复(现在真会出现"2 条长期记忆")，是 planner / 记忆侧的问题。ResponsePolicy 只做"确认已保存"，不做"合并"。
- **陈述句被路由成 memory.add 任务**：这是 planner 的决策，不在本层。
- **文采 / 风格**：本层只保证"回对类型"，自然表达仍交给 responder。

## 8. 可观测性

ResponsePolicy 的判断必须可见，否则又变成一个黑箱：

- `decide()` 返回的 `tiers` 和 `reason` 要进入 `Agent.run` 的 `result`，并被 `trace_view` 的 brief 模式打印。
- 例如：`[回复] tiers=[memory_confirmation, tool_summary] reason=保存了偏好且调用了 calculator`。
- 这样"最终回复为什么这么生成"就和 planner / memory 的 fallback 一样，一眼可查。

## 9. 测试

每一档配一个确定性测试(用假 responder / 合成 `ResponseContext`，不依赖真实 Ollama)：

- clarification：`needs_clarification` 的 decision → 回复是追问。
- safety：`sensitive` 的 decision → 回复是拒绝，且不含"已记住"之类措辞。
- safety + safe task：敏感记忆 + 安全工具任务 → 当前最小实现至少要说明敏感记忆未保存；未来增强后应继续完成安全任务。
- clarification + safe task：缺失记忆内容 + 安全工具任务 → 当前最小实现全局追问；未来增强后才支持先执行安全任务。
- 组合：trace 有工具成功 + `memory_decision.save=True` → 回复**同时**含两者。
- failure：trace 有失败 observation → 回复据实说明，不谎报完成。
- normal：空 trace + 无记忆动作 → 落到 responder。

## 10. 最小验收标准

实现后应该满足：

- 保存长期记忆后，最终回复能看到确认。
- 敏感信息不会被保存，也不会被 responder 美化成误导性回复。
- 工具执行结果不会被 responder 覆盖。
- 同时触发多个内容档时(工具结果 + 记忆确认)，回复应包含**全部**，不丢信息。
- 工具失败或任务未完成时，回复据实说明，不谎报完成。
- 普通聊天仍然可以使用 LLM responder。
- `ResponseDecision` 的 `tiers` / `reason` 出现在 trace 里，能复现每次回复的判断路径。

## 11. 一句话总结

`ResponsePolicy` 是 agent 的最终回复仲裁层。

它不追求文采，而是保证：

```text
该追问时追问
该拒绝时拒绝
该确认时确认
该总结工具结果时总结
普通聊天才交给 responder
```
