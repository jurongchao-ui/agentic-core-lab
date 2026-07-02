---
title: Agentic Core 加固开发日志
type: dev_log
date: 2026-07-02
scope: agentic_core
---

# Agentic Core 加固开发日志（2026-07-02）

本轮围绕一次代码评审展开，逐条修掉了发现的问题，并按"每步小改动 → 跑测试 → 看过程"的方式推进。全部改动零新依赖，风格与既有的"规则层 + LLM 层 + 程序把关"一致。收尾状态：**pytest 38 passed**。

---

## 改动清单（按完成顺序）

### 1. memory.add 网关化，堵住绕过 MemoryPolicy 的后门
- **问题**：`memory.add` 工具直接暴露给 LLM planner，模型可用任意 text + 自定义 scores 写长期记忆，绕过阈值和敏感检查。设计意图（"长期记忆由程序把关"）与实现矛盾。
- **修复**：[tools.py](agentic_core/tools.py) 把 `memory.add` 改成 `_memory_add` 方法，强制走 `MemoryPolicy.evaluate()`；模型只提议 text，是否保存/分类/评分全由 policy 决定。`ToolRegistry` 注入 `memory_policy`。
- **测试**：`tests/test_memory_add_gating.py`（含敏感信息拦截、忽略模型自评分）。

### 2. 工具参数 schema 单一真相源
- **问题**：新增工具要同时改三处（tools 注册、planner 的 `toolInputSchemas`、`validate_tool_input` 的 required），易漂移。
- **修复**：schema 挂到 `ToolRegistry` 注册处，`tools.list()` 携带 `inputSchema`；planner 的 prompt 提示和参数校验都从 `available_tools` 派生。纯重构，行为不变。
- **测试**：`tests/test_tool_schema_single_source.py`（含"注册即校验生效、不碰 planner"）。

### 3. MemoryPolicy 稳健化：LLM 抽取 + 规则兜底
- **问题**：用正则做语义判断，脆弱（"用 Python 算一下"误判成用户画像；"我是前端开发"又漏判）。
- **方案**：参考 mem0/Letta/LangMem 的共识（语义交给 LLM，程序把关），复用本项目 `HermesPlanner` 已有的"LLM 提议 → 程序校验 → 规则兜底"模式。
- **修复**：[memory_policy.py](agentic_core/memory_policy.py) 拆成基类 `MemoryPolicy` + `RuleBasedMemoryPolicy`（原逻辑，作 fallback）+ `LlmMemoryPolicy`（结构化抽取）。**关键控制点：敏感一票否决用程序侧正则做，不依赖模型**。敏感词提升为共享常量 `SENSITIVE_PATTERN`。装配层加 `AGENTIC_MEMORY_POLICY` 开关。抽出 [json_utils.py](agentic_core/json_utils.py) 共享 `extract_json_object`。
- **测试**：`tests/test_llm_memory_policy.py`（stub client，无需真实 Ollama）。

### 4. 修 confidence 解析崩溃（间歇性丢失记忆）
- **问题**：本地小模型把 `confidence` 返回成 null/非数字/0-1 小数时，`int()` 抛异常 → 静默回退规则版 → 规则版给"我是前端开发"打 5 分（<7）不保存。表现为间歇性没存。
- **修复**：新增 `coerce_confidence`——float 安全转换，无法解析用阈值默认值，0-1 量纲归一到 0-100，永不抛异常。
- **测试**：`test_malformed_confidence_does_not_crash`（None/"high"/0.9）。

### 5. 可观测性：捕获 LLM 原始输出 + 可读分步 trace
- **问题**：调试时"看不到过程"——LLM 原始输出从未被捕获，回退是静默的。
- **修复**：[schemas.py](agentic_core/schemas.py) 给 `Action`/`MemoryDecision` 加 `metadata`；两个 LLM 边界（`HermesPlanner`、`LlmMemoryPolicy`）改成"先存 raw 再解析"，成功/回退都写 `source + rawModelOutput + error`。新增 [trace_view.py](agentic_core/trace_view.py) 渲染可读分步。统一开关 `AGENTIC_TRACE=off|brief|json`（chat 默认 brief，cli 默认 json）。
- **测试**：`tests/test_trace_view.py` + metadata 捕获断言。

### 6. 加自然语言回复能力（它原来不回话）
- **问题**：这套系统是"任务 agent"，对闲聊（"你好…"）只回空的任务报告模板，不会回话。
- **修复**：新增 [responder.py](agentic_core/responder.py) 的 `LlmResponder`——职责分离：planner 只选工具，回话交给 responder。`Agent` 在"本轮没调用任何工具"时用 responder 生成自然回复。`validate_final_action` 已守住边界，只有真闲聊才触发。
- **测试**：`tests/test_responder.py`。

### 7. ResponsePolicy 最终回复仲裁层（设计 + 实现 + 评审）
- **设计**：先完善了 [response-policy-design.md](docs/response-policy-design.md)——拦截档（clarification/safety，择一即停）/ 内容档（memory confirmation + tool summary + failure，可组合）/ 兜底档（planner answer / responder）；补失败档；降级 Pydantic/LangGraph 为"需权衡的岔路"。
- **实现**：[response_policy.py](agentic_core/response_policy.py) 的 `ResponsePolicy.decide()` 返回可审计的 `ResponseDecision(text, tiers, reason)`，进入 `result` 并被 trace_view 打印。依赖失败计算不写笔记的判断在 ResponsePolicy 和 `RuleBasedPlanner` 双重把关。
- **测试**：`tests/test_response_policy.py`（每档一个确定性用例）。

### 8. 修 ResponsePolicy 敏感检测的脆弱耦合
- **问题**：LLM 记忆路径的敏感拒绝只写 `{"confidence": N}`，没有 `sensitivity_risk`，safety 档只能靠 `"敏感" in reason` 子串匹配——文案一改就静默失效，且无测试覆盖。
- **修复**：`LlmMemoryPolicy` 敏感拒绝时写入稳定信号 `sensitivity_risk=5`（与规则版一致）；`ResponsePolicy._is_sensitive_memory_rejection` 只认结构化信号，删掉子串匹配。
- **测试**：LLM 路径写入信号 + ResponsePolicy 无关键词也能触发 safety。

### 9. 堵住敏感信息泄漏进 note.add / todo.add
- **问题**（评审后端到端验证时发现）：长期记忆拦住了，但 LLM planner 转头调 `note.add` 把密码原文写进笔记，还被回显。写入类工具没有敏感检查。
- **修复**：[tools.py](agentic_core/tools.py) 在**工具执行层**（所有工具调用的唯一入口）加守卫——`_register` 增加 `guard_sensitive` 标记，`note.add`/`todo.add` 标为 True；`execute()` 执行前检查输入，命中 `SENSITIVE_PATTERN` 就 `raise`（变失败 observation，不落地），拒绝信息不回显原文。`memory.add` 本就经 policy 网关，无需改。
- **测试**：`tests/test_tool_sensitive_guard.py`（拒绝且不落地、错误不回显）。

---

## 当前链路

```text
Agent.run(goal)
  -> MemoryPolicy(evaluate)         # 规则版 或 LLM版(程序把关+敏感一票否决)
  -> [save long-term memory]
  -> Plan-Act-Observe loop
       Planner(next)                # HermesPlanner(LLM) -> RuleBasedPlanner(兜底)
       ToolRegistry.execute         # 写入类工具敏感守卫;单一 schema 真相源
       Observation
  -> ResponsePolicy.decide          # 拦截/组合/兜底分层,输出可审计 ResponseDecision
  -> Final Answer
```

可观测：`AGENTIC_TRACE=brief` 打印记忆决策(llm/fallback)、每步动作/工具结果、回退原因+模型原始输出、ResponseDecision 的 tiers/reason。

新增模块：`memory_policy.LlmMemoryPolicy` / `response_policy` / `responder` / `trace_view` / `json_utils`。

环境开关：`AGENTIC_MODEL` / `AGENTIC_PLANNER` / `AGENTIC_MEMORY_POLICY` / `AGENTIC_TRACE`（+ 兼容 `AGENTIC_CHAT_DEBUG`）。

---

## 遗留 / 未做（按优先级）

- **[高] 目录不是 git 仓库**：这么多安全相关改动零版本记录，建议尽早 `git init` 固化基线。
- **[中] `build_answer` 重复且近乎死代码**：`ResponsePolicy._summarize_tools` 与 planner 的 `build_answer` 两套工具汇总，且后者在新流程下 tier 6 几乎不可达。建议删或复用，二选一。
- **[中] `planner_answer` 档名存实亡**：有 responder 时闲聊轮会 null 掉它并走 responder，LLM planner 自己的 final.answer 永远被丢弃；且闲聊轮有两次 LLM 调用。
- **[中] 记忆去重**：agent 层保存 + memory.add 工具保存会产生重复长期记忆（ResponsePolicy 只是不重复播报，存储层未去重）。
- **[中] 持久化**：记忆仍在进程内，退出即失（roadmap Phase 2 的 JsonMemoryStore 未做）。
- **[中] LLM planner 在小模型上常回退**：`OllamaClient` 未用 Ollama 的 `format:"json"` 强制结构化输出，加上能大幅降低回退率。
- **[低] 陈述句被路由成 memory.add 任务**（planner 侧）；README 结构重复且未提"现在能闲聊"。

---

## 测试

```bash
cd /Users/jurongchao/Desktop/ai学习测试库/agentic
python3 -m pytest -q          # 38 passed
python3 -m compileall agentic_core examples tests
```

LLM 相关全部用 stub client 覆盖，不依赖真实 Ollama；真实 Ollama 仅用于端到端手动验证。
