"""Agentic Core Lab —— Python 主线包。

功能:
  一个可运行的最小 agentic runtime,用于学习"核心链路 + 生产化边界"。
  组件全部按 Protocol 契约(contracts)注入,可替换、可离线、可测试。

整体调用关系图(一次 Agent.run):
  cli / chat(装配层, 读环境变量组装组件)
      └─▶ Agent.run(goal)                                    (agent)
            ├─▶ SafetyPolicy.check       (safety_policy)   —— 有害请求拒整轮
            ├─▶ MemoryPolicy.evaluate    (memory_policy)   —— 是否存长期记忆(+敏感一票否决)
            ├─▶ MemoryStore              (memory)          —— 记忆读写/去重/生命周期 + 事件写入(event_writer)
            ├─▶ Planner.next             (planner)         —— 选下一个 Action(LLM/规则兜底)
            │     └─▶ MiddlewarePipeline + ToolRegistry.execute
            │              (middleware / tools)            —— 审批/成本/守卫 + 执行工具
            └─▶ ResponsePolicy.decide    (response_policy) —— 仲裁最终回复(可调 responder / tool_summary)
  身份/权限: runtime_context; LLM 客户端: ollama_client;
  数据结构: schemas; 角色接口: contracts;
  排障: trace_view(人读) / event_log(JSONL 时间线) / eval_harness(指标)。
"""

__all__ = [
    "agent",
    "memory",
    "memory_policy",
    "ollama_client",
    "planner",
    "schemas",
    "tools",
]
