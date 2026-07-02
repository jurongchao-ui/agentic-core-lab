from __future__ import annotations

import time
from typing import Any

from .contracts import (
    MemoryPolicy,
    Planner,
    PlannerContext,
    Responder,
    ResponsePolicy,
    SafetyPolicy,
)
from .memory import MemoryStore
from .response_policy import ResponseContext, ResponseDecision, RuleBasedResponsePolicy
from .safety_policy import RuleBasedSafetyPolicy
from .schemas import MemoryDecision, Observation, SafetyDecision
from .tools import ToolRegistry


class Agent:
    """Agent 是整个应用的“总导演”。

    它自己不负责理解自然语言,也不直接做计算/写笔记。
    它只负责编排这条核心链路:

        用户目标 -> 记忆判断 -> Planner 选动作 -> Tool 执行 -> 观察结果 -> 再规划/结束

    这就是 agentic application 里常说的 Plan-Act-Observe loop。
    """

    def __init__(
        self,
        planner: Planner,
        tools: ToolRegistry,
        memory: MemoryStore,
        memory_policy: MemoryPolicy,
        max_steps: int = 8,
        responder: Responder | None = None,
        response_policy: ResponsePolicy | None = None,
        safety_policy: SafetyPolicy | None = None,
    ) -> None:
        # planner: 决定下一步做什么。可以是 HermesPlanner,也可以是 RuleBasedPlanner。
        self.planner = planner

        # responder: 当本轮没有调用任何工具(纯闲聊/陈述)时,用它生成自然语言回复。
        # 可以为 None(例如离线 demo),此时闲聊仍走 planner 的任务报告模板。
        self.responder = responder

        # response_policy: 最终回复仲裁层。它决定“该追问、确认记忆、总结工具,
        # 还是交给 responder”,避免 responder 覆盖已经发生的系统事实。
        self.response_policy = response_policy or RuleBasedResponsePolicy()

        # safety_policy: 请求级全局安全拦截。命中即拒绝整轮,跳过记忆评估和 loop。
        self.safety_policy = safety_policy or RuleBasedSafetyPolicy()

        # tools: 工具注册表。Agent 不知道每个工具内部怎么实现,只按名字调用。
        self.tools = tools

        # memory: 保存笔记、待办、长期记忆、事件日志。
        self.memory = memory

        # memory_policy: 判断一句话是否值得进入长期记忆。
        self.memory_policy = memory_policy

        # max_steps 防止 agent 无限循环。例如模型一直不 final,最多也只跑 8 步。
        self.max_steps = max_steps

    def run(self, goal: str) -> dict[str, Any]:
        """运行一次 agent 任务。

        参数:
            goal: 用户输入的目标,例如“帮我计算 128 * 7, 然后记录成学习笔记”。

        返回:
            一个 dict,里面包含最终回答、trace、memory snapshot。

        初学者提示:
            dict[str, Any] 是 Python 类型标注,意思是“key 是字符串,value 可以是任意类型”。
            类型标注不会改变运行逻辑,主要帮助人和编辑器理解代码。
        """

        # run_id 用时间戳生成,用于把本次任务里的事件串起来。
        run_id = f"run_{int(time.time() * 1000)}"

        # trace 是 agent 的执行轨迹。每一步 action 和 observation 都会放进这里。
        # 后续 planner 会读取 trace,判断“已经做过什么,下一步该做什么”。
        trace: list[dict[str, Any]] = []

        # 0. 全局安全拦截: 有害请求直接拒绝整轮,跳过记忆评估和 loop。
        safety_decision = self.safety_policy.check(goal)
        if safety_decision.refuse:
            neutral = MemoryDecision(
                save=False, memory_type="none", text="", reason="被安全策略拦截", scores={}
            )
            response_decision = self._decide_response(
                goal=goal,
                memory_decision=neutral,
                saved_memories=[],
                trace=trace,
                planner_answer=None,
                incomplete_reason=None,
                safety_decision=safety_decision,
            )
            self.memory.record_event(
                {
                    "runId": run_id,
                    "type": "safety_refusal",
                    "safety": safety_decision.to_dict(),
                    "answer": response_decision.text,
                }
            )
            return {
                "runId": run_id,
                "answer": response_decision.text,
                "memoryDecision": neutral.to_dict(),
                "safetyDecision": safety_decision.to_dict(),
                "responseDecision": response_decision.to_dict(),
                "trace": trace,
                "memory": self.memory.snapshot(),
            }

        # 第一步先做记忆判断。
        # 注意: 这一步不是让模型自由决定,而是交给 MemoryPolicy 的规则层。
        memory_decision = self.memory_policy.evaluate(goal)
        saved_memories: list[dict[str, Any]] = []

        if memory_decision.needs_clarification:
            # 用户明确要求保存长期记忆,但没有提供具体内容时,直接追问。
            # 这一步必须发生在 planner 之前,避免模型或规则 planner 编造用户资料。
            response_decision = self._decide_response(
                goal=goal,
                memory_decision=memory_decision,
                saved_memories=saved_memories,
                trace=trace,
                planner_answer=None,
                incomplete_reason=None,
                safety_decision=safety_decision,
            )
            self.memory.record_event(
                {
                    "runId": run_id,
                    "type": "memory_clarification",
                    "decision": memory_decision.to_dict(),
                    "responseDecision": response_decision.to_dict(),
                    "answer": response_decision.text,
                }
            )
            return {
                "runId": run_id,
                "answer": response_decision.text,
                "memoryDecision": memory_decision.to_dict(),
                "safetyDecision": safety_decision.to_dict(),
                "responseDecision": response_decision.to_dict(),
                "trace": trace,
                "memory": self.memory.snapshot(),
            }

        if memory_decision.save:
            # 如果 MemoryPolicy 判断值得保存,就写入长期记忆。
            # 例如“以后安排学习任务时，每次控制在30分钟以内”。
            saved_memory = self.memory.add_long_term_memory(
                memory_type=memory_decision.memory_type,
                text=memory_decision.text,
                reason=memory_decision.reason,
                scores=memory_decision.scores,
            )
            saved_memories.append(saved_memory)
        else:
            # 如果只是临时状态,例如“我今天有点累”,就不保存。
            saved_memory = None

        # 不管是否保存,都记录一次 memory_decision 事件。
        # 这对学习和调试很重要: 我们能看到系统为什么保存/不保存。
        self.memory.record_event(
            {
                "runId": run_id,
                "type": "memory_decision",
                "decision": memory_decision.to_dict(),
                "savedMemory": saved_memory,
            }
        )

        # Plan-Act-Observe loop:
        # 每一轮让 planner 选择一个 action,然后 Agent 执行它。
        for step in range(1, self.max_steps + 1):
            # context 是 planner 做决策时能看到的“上下文包”。
            # 里面有用户目标、当前第几步、历史 trace、记忆、可用工具列表。
            context: PlannerContext = {
                "run_id": run_id,
                "goal": goal,
                "step": step,
                "trace": trace,
                "memory": self.memory,
                "available_tools": self.tools.list(),
            }

            # planner.next(context) 会返回一个 Action:
            # - type == "tool": 说明下一步要调用工具
            # - type == "final": 说明任务完成,可以回答用户
            action = self.planner.next(context)

            if action.type == "final":
                # final action 不再调用工具。
                # 最终回答统一交给 ResponsePolicy 仲裁。
                # 普通闲聊可以由 responder 生成;工具结果、记忆确认、失败说明不能被覆盖。
                planner_answer = action.answer if (trace or self.responder is None) else None
                response_decision = self._decide_response(
                    goal=goal,
                    memory_decision=memory_decision,
                    saved_memories=saved_memories,
                    trace=trace,
                    planner_answer=planner_answer,
                    incomplete_reason=None,
                    safety_decision=safety_decision,
                )
                self.memory.record_event(
                    {
                        "runId": run_id,
                        "type": "final",
                        "answer": response_decision.text,
                        "responseDecision": response_decision.to_dict(),
                    }
                )
                return {
                    "runId": run_id,
                    "answer": response_decision.text,
                    "memoryDecision": memory_decision.to_dict(),
                    "safetyDecision": safety_decision.to_dict(),
                    "responseDecision": response_decision.to_dict(),
                    "trace": trace,
                    "memory": self.memory.snapshot(),
                }

            # 如果 action 不是 final,那它就是 tool action。
            # started_at 用来计算工具耗时。
            started_at = time.time()
            observation = self._execute_action(action, started_at)

            # observation 是工具执行后的结果:
            # - ok=True 表示成功
            # - ok=False 表示失败,错误信息在 error 里
            trace_item = {
                "step": step,
                "action": action.to_dict(),
                "observation": observation.to_dict(),
            }
            trace.append(trace_item)

            if action.tool_name == "memory.add" and observation.ok:
                output = observation.output or {}
                if output.get("saved") and output.get("memory"):
                    saved_memories.append(output["memory"])

            # 事件日志和 trace 类似,但它存在 memory 里,用于最后 snapshot 展示。
            self.memory.record_event(
                {"runId": run_id, "step": step, "action": action.to_dict(), "observation": observation.to_dict()}
            )

        # 如果循环跑满 max_steps 还没有 final,说明任务没有正常结束。
        incomplete_reason = f"达到最大步数 {self.max_steps},任务未能自动完成。"
        response_decision = self._decide_response(
            goal=goal,
            memory_decision=memory_decision,
            saved_memories=saved_memories,
            trace=trace,
            planner_answer=None,
            incomplete_reason=incomplete_reason,
            safety_decision=safety_decision,
        )
        return {
            "runId": run_id,
            "answer": response_decision.text,
            "memoryDecision": memory_decision.to_dict(),
            "safetyDecision": safety_decision.to_dict(),
            "responseDecision": response_decision.to_dict(),
            "trace": trace,
            "memory": self.memory.snapshot(),
        }

    def _decide_response(
        self,
        goal: str,
        memory_decision: MemoryDecision,
        saved_memories: list[dict[str, Any]],
        trace: list[dict[str, Any]],
        planner_answer: str | None,
        incomplete_reason: str | None,
        safety_decision: SafetyDecision | None = None,
    ) -> ResponseDecision:
        """创建 ResponseContext,交给 ResponsePolicy 生成最终回复。"""
        return self.response_policy.decide(
            ResponseContext(
                goal=goal,
                memory_decision=memory_decision,
                saved_memories=list(saved_memories),
                trace=trace,
                planner_answer=planner_answer,
                incomplete_reason=incomplete_reason,
                memory_snapshot=self.memory.snapshot(),
                responder=self.responder,
                safety_decision=safety_decision,
            )
        )

    def _execute_action(self, action: Any, started_at: float) -> Observation:
        """执行一个 tool action,并把结果包装成 Observation。

        这里用 try/except 很关键:
        工具失败不应该让整个程序崩溃,而是变成 observation 交给下一轮 planner。
        这就是 Observe 的价值: agent 可以根据失败结果重新计划。
        """
        try:
            if not action.tool_name:
                raise ValueError("tool action requires tool_name")
            output = self.tools.execute(action.tool_name, action.input)
            return Observation(ok=True, output=output, elapsed_ms=int((time.time() - started_at) * 1000))
        except Exception as error:
            return Observation(
                ok=False,
                error=str(error),
                elapsed_ms=int((time.time() - started_at) * 1000),
            )
