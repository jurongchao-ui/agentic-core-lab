from __future__ import annotations

import time
import re
from typing import Any

from .contracts import (
    MemoryPolicy,
    Planner,
    PlannerContext,
    Responder,
    ResponsePolicy,
    SafetyPolicy,
)
from .memory import MemoryStore, now_iso
from .middleware import MiddlewarePipeline, ToolCallContext
from .response_policy import ResponseContext, ResponseDecision, RuleBasedResponsePolicy
from .safety_policy import RuleBasedSafetyPolicy
from .schemas import (
    Action,
    AgentRunResult,
    AgentRunState,
    MemoryDecision,
    MemoryRecord,
    Observation,
    RunStatus,
    SafetyDecision,
    TraceStep,
    skipped_memory_decision,
)
from .tools import ToolRegistry


class Agent:
    """Agent 是整个应用的“总导演”。

    它负责编排:

        用户目标 -> 安全判断 -> 记忆判断 -> Planner 选动作 -> Tool 执行 -> 观察结果 -> 最终回复

    run_typed() 是 typed 主入口; run() 只是为了兼容 CLI/Chat 的 dict 输出。
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
        middleware_pipeline: MiddlewarePipeline | None = None,
    ) -> None:
        self.planner = planner
        self.responder = responder
        self.response_policy = response_policy or RuleBasedResponsePolicy()
        self.safety_policy = safety_policy or RuleBasedSafetyPolicy()
        self.tools = tools
        self.memory = memory
        self.memory_policy = memory_policy
        self.max_steps = max_steps
        self.middleware_pipeline = middleware_pipeline or MiddlewarePipeline.default()

    def run(self, goal: str) -> dict[str, Any]:
        """兼容旧入口: 返回 CLI/Chat 已经习惯的 dict 结构。"""

        return self.run_typed(goal).to_dict()

    def run_typed(self, goal: str) -> AgentRunResult:
        """运行一次 agent 任务,并返回 typed result。

        初学者可以把 AgentRunState 理解成“本次运行的工作台”:
        trace、已保存记忆、当前状态都先放在这里,最后再打包成 AgentRunResult。
        """

        run_id = f"run_{int(time.time() * 1000)}"
        state = AgentRunState(
            run_id=run_id,
            goal=goal,
            status="running",
            started_at=now_iso(),
        )
        self._record_event(state, "run_started", {"goal": goal})
        safety_decision = SafetyDecision(refuse=False, category="none", reason="not checked")
        memory_decision: MemoryDecision | None = None
        try:
            return self._run_typed_body(state, goal, safety_decision, memory_decision)
        except Exception as error:
            return self._build_failed_result(
                state,
                goal,
                state.safety_decision or safety_decision,
                state.memory_decision or memory_decision,
                error,
            )

    def _run_typed_body(
        self,
        state: AgentRunState,
        goal: str,
        safety_decision: SafetyDecision,
        memory_decision: MemoryDecision | None,
    ) -> AgentRunResult:
        """run_typed 的主体逻辑。

        外层 run_typed 负责兜底 run_failed,这里专注正常链路。
        """

        safety_decision = self.safety_policy.check(goal)
        state.safety_decision = safety_decision
        self._record_event(
            state,
            "safety_decision",
            {"safety": safety_decision.to_dict()},
        )
        if safety_decision.refuse:
            response_decision = self._decide_response(
                goal=goal,
                memory_decision=None,
                saved_memories=[],
                trace=state.trace,
                planner_answer=None,
                incomplete_reason=None,
                safety_decision=safety_decision,
            )
            self._record_event(
                state,
                "safety_refusal",
                {
                    "safety": safety_decision.to_dict(),
                    "answer": response_decision.text,
                },
            )
            return self._build_result(
                state=state,
                status="refused",
                answer=response_decision.text,
                safety_decision=safety_decision,
                memory_decision=None,
                response_decision=response_decision,
            )

        memory_decision = self.memory_policy.evaluate(goal)
        state.memory_decision = memory_decision
        self._record_event(
            state,
            "memory_decision",
            {"decision": memory_decision.to_dict(), "savedMemory": None},
        )

        if memory_decision.needs_clarification:
            response_decision = self._decide_response(
                goal=goal,
                memory_decision=memory_decision,
                saved_memories=state.saved_memories,
                trace=state.trace,
                planner_answer=None,
                incomplete_reason=None,
                safety_decision=safety_decision,
            )
            self._record_event(
                state,
                "memory_clarification",
                {
                    "decision": memory_decision.to_dict(),
                    "responseDecision": response_decision.to_dict(),
                    "answer": response_decision.text,
                },
            )
            return self._build_result(
                state=state,
                status="clarification",
                answer=response_decision.text,
                safety_decision=safety_decision,
                memory_decision=memory_decision,
                response_decision=response_decision,
            )

        saved_memory: MemoryRecord | None = None
        if memory_decision.save:
            saved_memory = self.memory.add_long_term_memory(
                memory_type=memory_decision.memory_type,
                text=memory_decision.text,
                reason=memory_decision.reason,
                scores=memory_decision.scores,
            )
            state.saved_memories.append(saved_memory)
            self._record_event(
                state,
                "memory_saved",
                {"savedMemory": saved_memory.to_dict()},
            )

        if self._should_skip_planner(goal, memory_decision, state.saved_memories):
            response_decision = self._decide_response(
                goal=goal,
                memory_decision=memory_decision,
                saved_memories=state.saved_memories,
                trace=state.trace,
                planner_answer=None,
                incomplete_reason=None,
                safety_decision=safety_decision,
            )
            self._record_event(
                state,
                "planner_skipped",
                {
                    "reason": "No tool intent detected; response can be resolved by ResponsePolicy/responder.",
                    "responseDecision": response_decision.to_dict(),
                },
            )
            self._record_event(
                state,
                "response_decision",
                {
                    "answer": response_decision.text,
                    "responseDecision": response_decision.to_dict(),
                },
            )
            return self._build_result(
                state=state,
                status="completed",
                answer=response_decision.text,
                safety_decision=safety_decision,
                memory_decision=memory_decision,
                response_decision=response_decision,
            )

        for step in range(1, self.max_steps + 1):
            state.step = step
            context = PlannerContext(
                run_id=state.run_id,
                goal=goal,
                step=step,
                trace=state.trace,
                memory_snapshot=self.memory.snapshot(touch_long_term=True),
                available_tools=self.tools.list(),
            )
            action = self.planner.next(context)
            self._record_event(
                state,
                "planner_action",
                {"step": step, "action": action.to_dict()},
            )
            if action.source == "rule_fallback":
                self._record_event(
                    state,
                    "planner_fallback",
                    {
                        "step": step,
                        "action": action.to_dict(),
                        "reason": action.reason,
                        "metadata": action.metadata,
                    },
                )

            if action.type == "final":
                planner_answer = action.answer if (state.trace or self.responder is None) else None
                response_decision = self._decide_response(
                    goal=goal,
                    memory_decision=memory_decision,
                    saved_memories=state.saved_memories,
                    trace=state.trace,
                    planner_answer=planner_answer,
                    incomplete_reason=None,
                    safety_decision=safety_decision,
                )
                self._record_event(
                    state,
                    "response_decision",
                    {
                        "answer": response_decision.text,
                        "responseDecision": response_decision.to_dict(),
                    },
                )
                return self._build_result(
                    state=state,
                    status="completed",
                    answer=response_decision.text,
                    safety_decision=safety_decision,
                    memory_decision=memory_decision,
                    response_decision=response_decision,
                )

            started_at = time.time()
            self._record_event(
                state,
                "tool_started",
                {"step": step, "action": action.to_dict()},
            )
            observation = self._execute_action(state, action, started_at)
            trace_step = TraceStep(
                step=step,
                action=action,
                observation=observation,
                created_at=now_iso(),
            )
            state.trace.append(trace_step)

            if action.tool_name == "memory.add" and observation.ok:
                self._collect_memory_add_result(state, observation)

            self._record_event(
                state,
                "tool_observation",
                {
                    "step": step,
                    "action": action.to_dict(),
                    "observation": observation.to_dict(),
                },
            )

        incomplete_reason = f"达到最大步数 {self.max_steps},任务未能自动完成。"
        response_decision = self._decide_response(
            goal=goal,
            memory_decision=memory_decision,
            saved_memories=state.saved_memories,
            trace=state.trace,
            planner_answer=None,
            incomplete_reason=incomplete_reason,
            safety_decision=safety_decision,
        )
        self._record_event(
            state,
            "response_decision",
            {
                "answer": response_decision.text,
                "responseDecision": response_decision.to_dict(),
                "incompleteReason": incomplete_reason,
            },
        )
        return self._build_result(
            state=state,
            status="incomplete",
            answer=response_decision.text,
            safety_decision=safety_decision,
            memory_decision=memory_decision,
            response_decision=response_decision,
        )

    def _decide_response(
        self,
        goal: str,
        memory_decision: MemoryDecision | None,
        saved_memories: list[MemoryRecord],
        trace: list[TraceStep],
        planner_answer: str | None,
        incomplete_reason: str | None,
        safety_decision: SafetyDecision | None = None,
    ) -> ResponseDecision:
        """创建 ResponseContext,交给 ResponsePolicy 生成最终回复。"""

        return self.response_policy.decide(
            ResponseContext(
                goal=goal,
                memory_decision=memory_decision or skipped_memory_decision(),
                saved_memories=list(saved_memories),
                trace=trace,
                planner_answer=planner_answer,
                incomplete_reason=incomplete_reason,
                memory_snapshot=self.memory.snapshot(),
                responder=self.responder,
                safety_decision=safety_decision,
            )
        )

    def _execute_action(self, state: AgentRunState, action: Action, started_at: float) -> Observation:
        """执行一个 tool action,并把结果包装成 Observation。"""

        try:
            if not action.tool_name:
                raise ValueError("tool action requires tool_name")
            tool = self.tools.get_spec(action.tool_name)
            if not tool:
                raise ValueError(f"unknown tool: {action.tool_name}")
            context = ToolCallContext(
                run_id=state.run_id,
                step=state.step,
                action=action,
                tool=tool,
            )
            short_circuit = self.middleware_pipeline.before_tool(context)
            if short_circuit is not None:
                return short_circuit
            output = self.tools.execute(action.tool_name, action.input)
            observation = Observation(
                ok=True,
                output=output,
                elapsed_ms=int((time.time() - started_at) * 1000),
            )
            return self.middleware_pipeline.after_tool(context, observation)
        except Exception as error:
            return Observation(
                ok=False,
                error=str(error),
                elapsed_ms=int((time.time() - started_at) * 1000),
            )

    def _should_skip_planner(
        self,
        goal: str,
        memory_decision: MemoryDecision,
        saved_memories: list[MemoryRecord],
    ) -> bool:
        """判断本轮是否可以不进入 Planner。

        Planner 负责工具规划。若用户只是闲聊、陈述偏好、保存长期记忆或触发 local safety,
        ResponsePolicy/responder 已经能生成最终回复,不必再额外调用 LLM planner。
        """

        tool_intent_patterns = [
            r"\d+(?:\s*[+\-*/%]\s*\d+)+",
            r"记录|笔记|note",
            r"添加待办|新增待办|todo",
            r"列出待办|查看待办|list todo",
            r"学习计划|学习安排|学习规划|study plan",
        ]
        has_tool_intent = any(re.search(pattern, goal, re.I) for pattern in tool_intent_patterns)
        if has_tool_intent:
            return False
        has_direct_response_content = bool(saved_memories) or memory_decision.scores.get("sensitivity_risk", 0) >= 3
        return self.responder is not None or has_direct_response_content

    def _collect_memory_add_result(self, state: AgentRunState, observation: Observation) -> None:
        """memory.add 工具成功后,把真实写入的 MemoryRecord 加入 saved_memories。"""

        output = observation.output or {}
        if not isinstance(output, dict) or not output.get("saved"):
            return
        memory_data = output.get("memory") or {}
        memory_id = memory_data.get("id")
        for memory in self.memory.long_term_memories:
            if memory.id == memory_id:
                state.saved_memories.append(memory)
                return

    def _record_event(
        self,
        state: AgentRunState,
        event_type: str,
        payload: dict[str, Any],
        level: str = "info",
    ) -> None:
        event = self.memory.record_event(
            event_type=event_type,
            run_id=state.run_id,
            payload=payload,
            level=level,
        )
        state.events.append(event)

    def _build_result(
        self,
        state: AgentRunState,
        status: RunStatus,
        answer: str,
        safety_decision: SafetyDecision,
        memory_decision: MemoryDecision | None,
        response_decision: ResponseDecision,
    ) -> AgentRunResult:
        state.status = status
        self._record_event(
            state,
            "run_completed",
            {"status": status, "answer": answer},
        )
        return AgentRunResult(
            run_id=state.run_id,
            goal=state.goal,
            status=status,
            answer=answer,
            safety_decision=safety_decision,
            memory_decision=memory_decision,
            response_decision=response_decision,
            trace=list(state.trace),
            memory_snapshot=self.memory.snapshot(),
            events=list(state.events),
            started_at=state.started_at,
            completed_at=now_iso(),
        )

    def _build_failed_result(
        self,
        state: AgentRunState,
        goal: str,
        safety_decision: SafetyDecision,
        memory_decision: MemoryDecision | None,
        error: Exception,
    ) -> AgentRunResult:
        """未预期异常兜底。

        生产级 agent 不能让异常悄悄吞掉事件链。
        这里保证至少写出 run_failed,并返回结构化 failed 结果。
        """

        state.status = "failed"
        answer = "抱歉，当前任务执行失败，已记录失败事件，方便后续排查。"
        response_decision = ResponseDecision(
            text=answer,
            tiers=["run_failed"],
            reason=f"Agent run failed: {error}",
        )
        self._record_event(
            state,
            "run_failed",
            {
                "error": str(error),
                "errorType": error.__class__.__name__,
                "goal": goal,
            },
            level="error",
        )
        return AgentRunResult(
            run_id=state.run_id,
            goal=state.goal,
            status="failed",
            answer=answer,
            safety_decision=safety_decision,
            memory_decision=memory_decision,
            response_decision=response_decision,
            trace=list(state.trace),
            memory_snapshot=self.memory.snapshot(),
            events=list(state.events),
            started_at=state.started_at,
            completed_at=now_iso(),
        )
