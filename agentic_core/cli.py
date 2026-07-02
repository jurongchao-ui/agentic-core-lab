from __future__ import annotations

import json
import os
import sys

from .agent import Agent
from .memory import MemoryStore
from .memory_policy import LlmMemoryPolicy, RuleBasedMemoryPolicy
from .ollama_client import OllamaClient
from .planner import HermesPlanner, RuleBasedPlanner
from .responder import LlmResponder
from .tools import ToolRegistry
from .trace_view import format_run_brief, resolve_trace_mode


def main() -> int:
    """命令行入口。

    运行方式:
        python3 -m agentic_core.cli "帮我计算 128 * 7, 然后记录成学习笔记"

    这个函数负责:
        1. 读取命令行参数
        2. 组装 Agent 需要的组件
        3. 运行 agent
        4. 打印最终结果、记忆决策、trace 和 memory snapshot
    """

    # sys.argv 是命令行参数列表。
    # sys.argv[0] 是模块名/脚本名,真正的用户输入从 sys.argv[1:] 开始。
    goal = " ".join(sys.argv[1:]).strip()
    if not goal:
        print('Usage: python -m agentic_core.cli "帮我计算 128 * 7, 然后记录成学习笔记"')
        return 1

    # 环境变量允许你不改代码就切换配置:
    # AGENTIC_MODEL=openhermes:latest
    # AGENTIC_PLANNER=hermes 或 rule
    model = os.getenv("AGENTIC_MODEL", "openhermes:latest")
    planner_mode = os.getenv("AGENTIC_PLANNER", "hermes").lower()

    # 下面是在“组装应用”。
    # 这类写法也叫 dependency injection: 把组件创建好,再传给 Agent。
    # AGENTIC_MEMORY_POLICY=llm 用 LLM 抽取(默认,不可用时自动回退规则版),
    # =rule 则完全离线只用规则版。
    memory = MemoryStore()
    memory_policy = (
        LlmMemoryPolicy(OllamaClient(model=model))
        if os.getenv("AGENTIC_MEMORY_POLICY", "llm").lower() == "llm"
        else RuleBasedMemoryPolicy()
    )
    tools = ToolRegistry(memory, memory_policy)
    rule_planner = RuleBasedPlanner()

    # 默认使用 HermesPlanner。
    # 如果设置 AGENTIC_PLANNER=rule,就完全不用 Ollama,只跑规则 planner。
    planner = (
        HermesPlanner(OllamaClient(model=model), fallback=rule_planner)
        if planner_mode == "hermes"
        else rule_planner
    )
    # responder 让 agent 对闲聊也能自然回复(不可用时回退到能力引导语)。
    responder = LlmResponder(OllamaClient(model=model))
    agent = Agent(
        planner=planner,
        tools=tools,
        memory=memory,
        memory_policy=memory_policy,
        responder=responder,
    )
    result = agent.run(goal)

    # ensure_ascii=False 让 json.dumps 可以直接打印中文,不转成 \u4e2d 这种形式。
    # AGENTIC_TRACE=off|brief|json 控制过程可见度。cli 默认 json,保持原有完整输出。
    trace_mode = resolve_trace_mode("json")
    print("\n=== Final Answer ===")
    print(result["answer"])
    if trace_mode == "brief":
        print("\n=== Trace (brief) ===")
        print(format_run_brief(result))
    elif trace_mode == "json":
        print("\n=== Memory Decision ===")
        print(json.dumps(result["memoryDecision"], ensure_ascii=False, indent=2))
        print("\n=== Response Decision ===")
        print(json.dumps(result["responseDecision"], ensure_ascii=False, indent=2))
        print("\n=== Trace ===")
        print(json.dumps(result["trace"], ensure_ascii=False, indent=2))
        print("\n=== Memory Snapshot ===")
        print(json.dumps(result["memory"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
