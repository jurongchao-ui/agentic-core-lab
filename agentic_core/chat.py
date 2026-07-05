"""chat — 连续对话入口(装配层)。

功能:
  - 和 cli 的关键区别: 只在启动时创建一次 MemoryStore,多轮输入复用它 —— 这就是
    "能记住上一轮"的原因。
  - REPL 循环读用户输入, 每轮跑一次 Agent.run, 按 trace_mode 打印过程/答案。
  - 兼容 AGENTIC_CHAT_DEBUG=1(等价 AGENTIC_TRACE=json); 两行输入模式缓解终端中文残留。

调用关系图:
  python -m agentic_core.chat
      └─▶ build_agent()  —— 一次性装配(同 cli 的组件, 但 MemoryStore 只建一次)
      └─▶ while True: input() ─▶ Agent.run(goal) ─▶ print_trace(result, mode)
                                                    (trace_view.format_run_brief/json)
"""

from __future__ import annotations

import os

# 只要导入 readline,Python 的 input() 在很多终端里就会获得更好的行编辑能力。
# 如果某些 Python 环境没有 readline,也不影响程序运行。
try:
    import readline  # noqa: F401
except ImportError:  # pragma: no cover
    readline = None  # type: ignore[assignment]

from agentic_core.runtime.agent import Agent
from agentic_core.memory.store import build_memory_store_from_env
from agentic_core.policies.memory import LlmMemoryPolicy, RuleBasedMemoryPolicy
from agentic_core.llm.ollama_client import OllamaClient
from agentic_core.policies.planner import HermesPlanner, RuleBasedPlanner
from agentic_core.policies.responder import LlmResponder
from agentic_core.runtime.context import build_runtime_identity_from_env
from agentic_core.policies.safety import build_safety_policy_from_env
from agentic_core.policies.safety_review import build_safety_review_queue_from_env
from agentic_core.tools.middleware import build_middleware_pipeline_from_env
from agentic_core.tools.registry import ToolRegistry
from agentic_core.observability.trace_view import format_run_brief, format_run_json, resolve_trace_mode


EXIT_COMMANDS = {"exit", "quit", "退出"}


def build_agent() -> Agent:
    """创建一个可用于多轮对话的 Agent。

    和 cli.py 最大的区别是:
        cli.py 每运行一次就创建一次 MemoryStore。
        chat.py 只在启动时创建一次 MemoryStore,然后多轮对话复用它。

    这就是连续对话能“记住上一轮”的关键。
    """

    model = os.getenv("AGENTIC_MODEL", "openhermes:latest")
    planner_mode = os.getenv("AGENTIC_PLANNER", "hermes").lower()

    memory = build_memory_store_from_env()
    memory_policy = (
        LlmMemoryPolicy(OllamaClient(model=model))
        if os.getenv("AGENTIC_MEMORY_POLICY", "llm").lower() == "llm"
        else RuleBasedMemoryPolicy()
    )
    tools = ToolRegistry(memory, memory_policy)
    rule_planner = RuleBasedPlanner()
    planner = (
        HermesPlanner(OllamaClient(model=model), fallback=rule_planner)
        if planner_mode == "hermes"
        else rule_planner
    )

    responder = LlmResponder(OllamaClient(model=model))
    return Agent(
        planner=planner,
        tools=tools,
        memory=memory,
        memory_policy=memory_policy,
        responder=responder,
        safety_policy=build_safety_policy_from_env(model=model),
        safety_review_queue=build_safety_review_queue_from_env(),
        middleware_pipeline=build_middleware_pipeline_from_env(),
        identity=build_runtime_identity_from_env(),
    )


def print_memory_summary(result: dict) -> None:
    """打印一行学习友好的记忆状态摘要。"""

    memory = result["memory"]
    print(
        "状态: "
        f"{len(memory['longTermMemories'])} 条长期记忆, "
        f"{len(memory['notes'])} 条笔记, "
        f"{len(memory['todos'])} 条待办。"
    )


def print_trace(result: dict, mode: str) -> None:
    """按 trace_mode 打印过程。brief=可读分步, json=完整 JSON, off=不打印。"""

    if mode == "brief":
        print("\n" + format_run_brief(result))
    elif mode == "json":
        print("\n" + format_run_json(result))


def main() -> int:
    """连续对话入口。

    运行:
        python3 -m agentic_core.chat

    输入 exit / quit / 退出 结束。
    设置 AGENTIC_CHAT_DEBUG=1 可以看到每轮的记忆判断和 trace。
    """

    # trace_mode: chat 默认 brief(过程默认可见)。AGENTIC_CHAT_DEBUG=1 向后兼容,等价 json。
    trace_mode = "json" if os.getenv("AGENTIC_CHAT_DEBUG") == "1" else resolve_trace_mode("brief")
    prompt = os.getenv("AGENTIC_CHAT_PROMPT", "User>")
    inline_prompt = os.getenv("AGENTIC_CHAT_INLINE_PROMPT") == "1"
    agent = build_agent()

    print("Agentic Core Chat")
    print("输入 exit / quit / 退出 结束。")
    print(f"输入提示符: {prompt!r}")
    memory_path = os.getenv("AGENTIC_MEMORY_PATH", "data/memory.json")
    if os.getenv("AGENTIC_MEMORY_STORE", "memory").lower() == "json" or os.getenv("AGENTIC_MEMORY_PATH"):
        print(f"当前记忆会持久化到 {memory_path}。")
    else:
        print("当前记忆只保存在本次 chat 进程内,退出后会消失。")
    print(f"Trace: {trace_mode} (设 AGENTIC_TRACE=off|brief|json 切换)")
    print()

    while True:
        try:
            # 默认使用“两行输入”:
            #   User>
            #   这里输入中文
            #
            # 这样中文内容从干净的新行开始,能减少某些终端里退格/删除后的显示残留。
            # 如果你想恢复单行提示符,可以设置 AGENTIC_CHAT_INLINE_PROMPT=1。
            if inline_prompt:
                user_message = input(f"{prompt} ").strip()
            else:
                print(prompt)
                user_message = input().strip()
        except (EOFError, KeyboardInterrupt):
            print("\nAgent> 已退出。")
            return 0

        if not user_message:
            continue

        if user_message.lower() in EXIT_COMMANDS:
            print("Agent> 已退出。")
            return 0

        result = agent.run(user_message)
        print("\nAgent>")
        print(result["answer"])
        print_memory_summary(result)
        print_trace(result, trace_mode)
        print()


if __name__ == "__main__":
    raise SystemExit(main())
