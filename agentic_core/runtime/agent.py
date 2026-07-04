"""agent — 编排中枢(Plan-Act-Observe loop 的"总导演")。

功能:
  - Agent 自己不理解自然语言、也不直接算/写记忆; 只按契约编排各角色, 并把每一步
    记成结构化事件(EventRecord)。
  - run_typed(goal) 返回强类型 AgentRunResult; run(goal) 返回等价 dict(兼容 cli/chat)。
  - 依赖全部按 Protocol 注入(planner/memory_policy/responder/response_policy/safety_policy/
    middleware_pipeline/identity), 可替换、可离线、可测。
  - 若本轮无工具意图(闲聊/纯记忆确认)则 _should_skip_planner 跳过 Planner, 避免双 LLM 调用。

一次 run 的调用关系图:
  Agent.run_typed(goal) ─▶ _run_typed_body
    1) SafetyPolicy.check(goal)            —— refuse 则 global_safety, 跳过下面一切
    2) MemoryPolicy.evaluate(goal)         —— 决定是否存长期记忆(+敏感一票否决)
       └─▶ MemoryStore.add_long_term_memory (若 save)
    3) for step in loop:
         Planner.next(PlannerContext)      —— 选 Action(tool / final)
         MiddlewarePipeline.execute_tool ─▶ ToolRegistry.execute   —— 审批/成本/守卫 + 执行
         └─▶ Observation ─▶ TraceStep
    4) ResponsePolicy.decide(ResponseContext) ─▶ ResponseDecision  —— 仲裁最终回复
  每步都 _record_event ─▶ MemoryStore.record_event ─▶ EventWriter(内存/JSONL/SQLite)
  产物 AgentRunResult ─▶ cli/chat 用 trace_view 展示; eval_harness 读事件算指标。
"""

from __future__ import annotations

import time
import re
from typing import Any

from agentic_core.runtime.contracts import (
    MemoryPolicy,
    Planner,
    PlannerContext,
    Responder,
    ResponsePolicy,
    SafetyPolicy,
)
from agentic_core.observability.event_payloads import (
    EventPayloadInput,
    MemoryClarificationPayload,
    MemoryDecisionPayload,
    MemorySavedPayload,
    PlannerActionPayload,
    PlannerFallbackPayload,
    PlannerSkippedPayload,
    ResponseDecisionPayload,
    RunCompletedPayload,
    RunFailedPayload,
    RunStartedPayload,
    SafetyDecisionPayload,
    SafetyRefusalPayload,
    ToolObservationPayload,
    ToolStartedPayload,
)
from agentic_core.memory.store import MemoryStore, now_iso
from agentic_core.tools.middleware import MiddlewarePipeline, ToolCallContext
from agentic_core.policies.response import ResponseContext, ResponseDecision, RuleBasedResponsePolicy
from agentic_core.runtime.context import RuntimeIdentity
from agentic_core.policies.safety import RuleBasedSafetyPolicy
from agentic_core.runtime.schemas import (
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
from agentic_core.tools.registry import ToolRegistry


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
        identity: RuntimeIdentity | None = None,
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
        self.identity = identity or RuntimeIdentity()

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
            identity=self.identity,
        )
        self._record_event(state, "run_started", RunStartedPayload(goal=goal, identity=self.identity.to_dict()))
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
            SafetyDecisionPayload(safety=safety_decision.to_dict()),
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
                SafetyRefusalPayload(
                    safety=safety_decision.to_dict(),
                    answer=response_decision.text,
                ),
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
            MemoryDecisionPayload(decision=memory_decision.to_dict(), saved_memory=None),
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
                MemoryClarificationPayload(
                    decision=memory_decision.to_dict(),
                    response_decision=response_decision.to_dict(),
                    answer=response_decision.text,
                ),
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
                user_id=self.identity.user_id,
                tenant_id=self.identity.tenant_id,
            )
            state.saved_memories.append(saved_memory)
            self._record_event(
                state,
                "memory_saved",
                MemorySavedPayload(saved_memory=saved_memory.to_dict()),
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
                PlannerSkippedPayload(
                    reason="No tool intent detected; response can be resolved by ResponsePolicy/responder.",
                    response_decision=response_decision.to_dict(),
                ),
            )
            self._record_event(
                state,
                "response_decision",
                ResponseDecisionPayload(
                    answer=response_decision.text,
                    response_decision=response_decision.to_dict(),
                ),
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
                memory_snapshot=self._memory_snapshot(touch_long_term=True),
                available_tools=self.tools.list(),
            )
            action = self.planner.next(context)
            self._record_event(
                state,
                "planner_action",
                PlannerActionPayload(step=step, action=action.to_dict()),
            )
            if action.source == "rule_fallback":
                self._record_event(
                    state,
                    "planner_fallback",
                    PlannerFallbackPayload(
                        step=step,
                        action=action.to_dict(),
                        reason=action.reason,
                        metadata=action.metadata,
                    ),
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
                    ResponseDecisionPayload(
                        answer=response_decision.text,
                        response_decision=response_decision.to_dict(),
                    ),
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
                ToolStartedPayload(step=step, action=action.to_dict()),
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
                ToolObservationPayload(
                    step=step,
                    action=action.to_dict(),
                    observation=observation.to_dict(),
                ),
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
            ResponseDecisionPayload(
                answer=response_decision.text,
                response_decision=response_decision.to_dict(),
                incomplete_reason=incomplete_reason,
            ),
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
                memory_snapshot=self._memory_snapshot(),
                responder=self.responder,
                safety_decision=safety_decision,
            )
        )

    def _memory_snapshot(self, touch_long_term: bool = False) -> Any:
        return self.memory.snapshot(
            touch_long_term=touch_long_term,
            user_id=self.identity.user_id,
            tenant_id=self.identity.tenant_id,
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
                identity=state.identity,
            )
            return self.middleware_pipeline.execute_tool(
                context,
                lambda: self.tools.execute(action.tool_name or "", action.input),
            )
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
        payload: EventPayloadInput,
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
            RunCompletedPayload(status=status, answer=answer, identity=state.identity.to_dict()),
        )
        return AgentRunResult(
            run_id=state.run_id,
            goal=state.goal,
            status=status,
            answer=answer,
            identity=state.identity,
            safety_decision=safety_decision,
            memory_decision=memory_decision,
            response_decision=response_decision,
            trace=list(state.trace),
            memory_snapshot=self._memory_snapshot(),
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
            RunFailedPayload(
                error=str(error),
                error_type=error.__class__.__name__,
                goal=goal,
                identity=state.identity.to_dict(),
            ),
            level="error",
        )
        return AgentRunResult(
            run_id=state.run_id,
            goal=state.goal,
            status="failed",
            answer=answer,
            identity=state.identity,
            safety_decision=safety_decision,
            memory_decision=memory_decision,
            response_decision=response_decision,
            trace=list(state.trace),
            memory_snapshot=self._memory_snapshot(),
            events=list(state.events),
            started_at=state.started_at,
            completed_at=now_iso(),
        )
