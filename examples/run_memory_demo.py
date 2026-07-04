from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentic_core.runtime.agent import Agent
from agentic_core.memory.store import MemoryStore
from agentic_core.policies.memory import RuleBasedMemoryPolicy
from agentic_core.policies.planner import RuleBasedPlanner
from agentic_core.tools.registry import ToolRegistry


def run(goal: str) -> None:
    memory = MemoryStore()
    policy = RuleBasedMemoryPolicy()
    agent = Agent(
        planner=RuleBasedPlanner(),
        tools=ToolRegistry(memory, policy),
        memory=memory,
        memory_policy=policy,
    )
    result = agent.run(goal)
    print(goal)
    print(result["memoryDecision"])
    print(result["memory"]["longTermMemories"])
    print()


if __name__ == "__main__":
    run("我今天有点累")
    run("以后安排学习任务时，每次控制在30分钟以内")
