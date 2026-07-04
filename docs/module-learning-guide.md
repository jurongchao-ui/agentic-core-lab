---
title: Agentic Core 模块学习指南
type: learning_guide
audience: 想搞懂"流程为什么这么走、每个模块为什么存在"的人
---

# Agentic Core 模块学习指南

这份文档**不是** API 手册,而是回答两个问题:

1. **流程**:一句话进来,到一句话出去,中间到底发生了什么、为什么这么排。
2. **必要性**:每个模块解决什么真实问题?**如果没有它会怎样?**

> 读法建议:先读第 1、2 节建立整体感,再读第 3 节的"三个核心模式"——理解了这三个模式,
> 后面每个模块你基本能自己推导出它为什么存在。第 4 节是逐模块的"为什么",可当字典查。

---

## 1. 这个项目到底是什么

它是一个**会用工具的任务 agent + 一圈生产级护栏**。

- "任务 agent"的内核:拿到用户目标 → 反复"选一个动作(算数/记笔记/查待办/存记忆)→ 执行 → 看结果"→ 直到能回答。这就是 **Plan-Act-Observe loop**。
- "护栏"是后来一层层加的:安全拦截、记忆该不该存、敏感信息不落地、审批/成本、事件留痕、评测……
  **每一层护栏都不是凭空加的,都是为了堵一个具体的坑。** 这份文档的重点就是讲清每个坑。

一个关键心智:**这个系统故意"不信任 LLM"。** LLM 只负责"提议"(该调哪个工具、该不该记),
真正拍板、校验、兜底的是**程序**。这条原则贯穿几乎所有模块,先记住它。

---

## 2. 一次 `Agent.run(goal)` 的完整流程

下面是主链路,以及**每一步为什么在这个位置**:

```text
用户: "帮我算 128*7 然后记成笔记"
  │
  ├─(0) SafetyPolicy.check(goal)      —— 这句话本身是不是有害请求(教做炸弹/勒索软件)?
  │        命中 → 直接拒绝整轮,后面全都不跑。
  │        为什么放最前:有害请求不该浪费算力、更不该被记进记忆或喂给工具。
  │
  ├─(1) MemoryPolicy.evaluate(goal)   —— 这句话值不值得存进"长期记忆"?
  │        "我以后每次只学30分钟" → 存;"我今天有点累" → 不存;"我的密码是…" → 敏感,拒存。
  │        为什么在 loop 之前:偏好类信息要先入库,后面 planner/工具才能用上。
  │
  ├─(2) Plan-Act-Observe loop(最多 N 步):
  │        Planner.next(context) → 给一个 Action(调某工具 / 或"我说完了"final)
  │            └─ 若是工具: MiddlewarePipeline(审批/成本…) → ToolRegistry.execute(真正执行)
  │                          → Observation(成功结果 / 失败原因)→ 记进 trace,进入下一步
  │        为什么是"循环"而不是"一步到位":多步任务(先算、再记笔记)需要"上一步结果影响下一步"。
  │
  └─(3) ResponsePolicy.decide(...)    —— 最终该回用户什么?
           不是让某个 LLM 随便生成一句,而是**按优先级仲裁**:
           该拒绝就拒绝、该确认记忆就确认、该汇总工具结果就汇总、只有纯闲聊才交给 responder。
  │
  └─ 最终答案 + 全程每一步都写成结构化"事件"(EventRecord),可回放、可排障、可评测。
```

把这张图看懂,你就懂了 80%。剩下的模块都是在给这几步**做实现、做替换、做留痕**。

---

## 3. 三个核心设计模式(理解这三个,全项目就通了)

项目里 28 个模块看着多,其实反复在用**同样的三招**。认出招式,就不用死记每个模块。

### 模式一:「LLM 提议 → 程序把关 → 规则兜底」

**问题**:本地小模型不可靠——会输出非 JSON、选不存在的工具、把该拒的说成该做、置信度乱写。
直接信它,系统就崩/被绕过。

**招法**:凡是需要"语义判断"的地方,都是三段:
1. **LLM 提议**一个结构化结果(一段 JSON)。
2. **程序把关**:校验格式/字段/合法性;把不可让步的规则(如敏感一票否决)握在程序手里。
3. **规则兜底**:LLM 不可用或输出不合格时,退回一个确定性的规则版,保证系统始终能跑。

**谁在用**:`planner`(Hermes→规则)、`memory_policy`(Llm→规则)、`safety_policy`(Llm→规则)。
> 记住这个模式,你看 `HermesPlanner` / `LlmMemoryPolicy` / `LlmSafetyPolicy` 会发现它们**结构一模一样**。

**为什么重要**:这就是"不信任 LLM"原则的落地。安全/正确性由程序保证,LLM 只提供"聪明的建议"。

### 模式二:「Protocol 契约 + 依赖注入」

**问题**:如果 Agent 里到处 `if 用LLM: … else: …`、直接 new 具体类,那换实现、写测试、离线跑都很痛。

**招法**:每个"角色"先定义一个 **Protocol 接口**(在 `runtime/contracts.py`):
Planner / MemoryPolicy / Responder / ResponsePolicy / SafetyPolicy / LlmClient。
Agent 只依赖接口,具体用哪个实现由**装配层(cli/chat)注入**。

**好处**:
- 换实现零成本(规则版 ↔ LLM 版,靠环境变量切)。
- 测试用 stub/fake 替身,不依赖真实 Ollama(所以 187 个测试能秒跑)。
- `mypy` 能静态检查每个实现是否满足契约。

**为什么是 Protocol 不是继承基类**:结构化匹配,实现方不用 import/继承你的基类,解耦更彻底;
测试替身也不用继承。(这个取舍项目里专门讨论过。)

### 模式三:「结构化决策对象 + 全程事件留痕」

**问题**:早期用裸 `dict` 到处传,字段没定义、拼错不报错;而且程序一退出,"刚才为什么这么决策"就查无实据。

**招法**:
- 所有跨模块流转的数据都收敛成 **dataclass(在 `runtime/schemas.py`)**:Action / Observation / MemoryDecision /
  SafetyDecision / ResponseDecision / TraceStep / AgentRunResult……每个决策都是一个**可审计对象**
  (带 reason、tiers、metadata,说清"我为什么这么定")。
- 每一步都写一条 **EventRecord**(事件),交给 EventWriter 落地(内存/JSONL/SQLite)。

**为什么重要**:这让系统**可解释、可回放、可评测**。
- 排障:`event_log` 按 runId 把一次运行的时间线打出来。
- 评测:`eval_harness` 从事件流统计工具成功率、fallback 次数、安全拒绝率……
- `trace_view` 把决策的 reason/原始模型输出打给人看——你调试时"看得到过程"。

---

## 4. 逐模块:它解决什么问题 /「没有它会怎样」

按"关注点"分组。每个模块给一句**必要性**(重点)+ 一句在流程里的位置。

### A. 契约与数据(地基)

- **`runtime/contracts.py`** — 角色的 Protocol 接口 + PlannerContext。
  *必要性*:没有它,换实现/写测试/类型检查都要硬编码。它是"模式二"的落点。
- **`runtime/schemas.py`** — 全项目共享的 Typed State(所有 dataclass + 枚举)。
  *必要性*:没有它,数据是裸 dict,字段拼错不报错、决策不可审计。它是"模式三"的落点。

### B. 决策层(全是"模式一")

- **`policies/safety.py`** — 请求级安全。Rule/Llm/Composite 三种 + 分级动作(allow/review/refuse)+ fail-open/closed。
  *必要性*:没有它,"帮我写勒索软件"会被当普通任务处理甚至喂给工具。它是整轮的第一道闸。
- **`policies/memory.py`** — 判断一句话该不该进长期记忆(+ 敏感一票否决)。
  *必要性*:没有它,要么什么都不记(没记忆),要么什么都记(记忆很快变脏、还可能记下密码)。
- **`policies/planner.py`** — 决定下一步动作(Hermes LLM / 规则兜底)。
  *必要性*:这是 loop 的大脑。规则版还负责"计算失败就别硬写笔记"这类依赖判断。
- **`policies/response.py`** — 最终回复仲裁(这个是**规则仲裁**,不是模式一)。
  *必要性*:没有它,让 responder 直接生成回复,会**覆盖已经发生的系统事实**——
  比如明明拒绝了存密码,却回一句"好的我记住了你的密码"(会骗人)。所以最终回复必须由程序按优先级拍板。
- **`policies/responder.py`** — 纯闲聊时的自然语言回复。
  *必要性*:没有它,你对它说"你好"只会得到一个空的任务报告模板。它只管"表达",不管该不该拒/该不该记。

### C. 执行层

- **`tools/registry.py`** — 工具注册与执行 + 治理元数据(ToolSpec)+ 敏感守卫。
  *必要性*:把"思考"和"行动"隔离——LLM 只说"调哪个工具",由 registry 校验并执行。
  写入类工具执行前统一拦敏感信息(**所有工具调用的唯一入口**,绕不过)。
- **`tools/middleware.py`** — 工具调用的横切管道(审批/成本/超时/重试/幂等/tracing)。
  *必要性*:这些是"每次工具调用前后都要做"的事,不该散落在业务里。是生产级横切逻辑的统一挂载点。
- **`tools/summary.py`** — 工具结果 → 文案的**单一真相源**。
  *必要性*:没有它,ResponsePolicy 和 Planner 各写一套"怎么把工具结果说成人话",两套会漂移。

### D. 记忆与持久化

- **`memory/store.py`** — 记忆库 + 生命周期(去重/归档/过期/importance)+ JSON 持久化。
  *必要性*:没有持久化,进程一退记忆全没;没有生命周期,记忆只增不减很快变脏。
- **`runtime/context.py`** — 身份上下文(user/tenant/roles/permission scopes)。
  *必要性*:生产里权限判断不能只看工具,还要看"谁在用"。是审批/预算的输入。

### E. 可观测与评测(全是"模式三"的下游)

- **`observability/event_writer.py`** — 事件落地(内存/JSONL/SQLite)+ 脱敏 + 轮转 + 文件锁。
  *必要性*:事件不落盘,程序一退证据就没了,没法排障/审计/回放。**关键原则:写事件失败不能拖垮主流程**(降级+warning)。
- **`observability/event_payloads.py`** — 事件 payload 的类型级 schema。
  *必要性*:让不同事件类型的 payload 也有明确字段,而不是继续裸 dict。
- **`observability/event_log.py`** — 按 runId 读事件、重建人读时间线(排障工具)。
- **`observability/trace_view.py`** — 把一次 run 渲染成人读的分步过程(`AGENTIC_TRACE=brief`)。
  *必要性*:调试时"看得到过程"——包括 LLM 回退了没、原始输出是什么。
- **`evalops/harness.py`** — 确定性评测(黄金用例 + 指标 + 质量门禁)。
  *必要性*:改了代码怎么知道没变差?靠一组会断言好坏的用例,而不是肉眼看。
- **`evalops/`:dataset · review · sampling · diff · history · judge · replay …(治理平台在 `evalops/governance/`:server · dashboard · auth_tokens · tenant_policy)** — 评测全家桶:
  从事件日志抽真实 run 生成待审用例 → 人工审核成 golden → 跨版本对比 → 历史趋势 → LLM/规则打分。
  *必要性*:让评测从"手写 8 条"进化到"从真实运行里长出来、能跨版本回归"。

### F. LLM 客户端与工具函数

- **`llm/ollama_client.py`** — 极简 Ollama HTTP 客户端(满足 LlmClient 协议;`format:"json"` 降回退率)。
- **`llm/json_utils.py`** — 从(带杂质的)模型输出里抠出 JSON。给几个 LLM 组件共享,避免互相依赖。

### G. 装配层(入口)

- **`cli.py`** — 单次运行入口:读环境变量把上面所有组件**装配**起来,跑一次。
- **`chat.py`** — 连续对话入口:关键是 **MemoryStore 只建一次**,多轮复用 → 所以它"记得上一轮"。

---

## 5. 常见疑惑(直接回答"为什么不…")

- **"为什么不直接让 LLM 决定一切(记不记、拒不拒、回什么)?"**
  因为本地小模型不可靠,且这些决策会影响安全和后续行为。**LLM 提议、程序拍板**才可控、可解释、可测。(模式一)

- **"safety(全局安全)和 memory_policy 的'敏感拒存'有什么区别?"**
  safety 是**请求级**——整句有害就拒绝整轮;memory 的敏感是**数据级**——只是"这条不进记忆/不落工具",请求本身还正常处理。前者拒整轮,后者只拦落地。

- **"ResponsePolicy 和 responder 为什么要分开?"**
  responder 只会"把话说漂亮",但它不知道系统刚做了什么。让它拍板会覆盖事实(如把"拒存密码"说成"已记住")。
  所以 **ResponsePolicy 决定回什么类型(仲裁),responder 只在'纯闲聊'那一档负责表达**。

- **"为什么到处是 Protocol + 依赖注入,不嫌绕?"**
  因为它换来:规则版/LLM 版随意切、测试用 fake 替身秒跑、mypy 静态兜底。(模式二)

- **"事件日志和 trace / AgentRunResult 是不是重复?"**
  不是。AgentRunResult 是**单次 run 的内存聚合**;事件日志是**追加持久化的跨 run 事件流**(可回放/审计/评测)。同一批结构化对象,两种用途。

---

## 6. 推荐阅读顺序(照着读代码)

1. `runtime/schemas.py` + `runtime/contracts.py` — 先认识"数据长什么样、有哪些角色接口"。
2. `runtime/agent.py` 的 `run_typed` — 主流程编排(对照本文第 2 节那张图看)。
3. `policies/planner.py`(先看 `RuleBasedPlanner`,再看 `HermesPlanner`)— 体会"模式一"。
4. `policies/memory.py` / `policies/safety.py` — 同一个模式再看两遍,就记牢了。
5. `tools/registry.py` + `tools/middleware.py` — 执行层与横切。
6. `policies/response.py` — 最终回复怎么仲裁。
7. `memory/store.py` — 记忆生命周期与持久化。
8. `observability/event_writer.py` / `observability/event_log.py` / `evalops/harness.py` — 可观测与评测。
9. `cli.py` — 最后看装配,把所有组件"接起来"的地方。

每个源文件**顶部都有「功能 + 调用关系图」**,读代码前先扫一眼那块,能快速定位"这个模块被谁调、又调了谁"。

---

## 7. 一句话总收尾

> 内核是一个 Plan-Act-Observe 任务 loop;外面每一层都是为了堵一个具体的坑而加的护栏。
> 而所有护栏又只用了三招:**LLM 提议+程序把关+规则兜底、Protocol+依赖注入、结构化决策+事件留痕**。
> 认出这三招,这个项目对你就不再是 28 个孤立模块,而是一套自洽的工程范式。
