"""tools — 工具注册与执行(把"思考"和"行动"隔离开)。

功能:
  - ToolRegistry 注册内置工具(calculator/note.add/todo.add/todo.list/memory.add/study.plan),
    LLM/Planner 只输出"调哪个工具+参数", 由 registry 找到并执行 —— 系统更可控。
  - ToolSpec: 类型化的工具定义 + 治理元数据(input_schema=参数真相源、side_effect、
    guard_sensitive、以及 timeout/retry/cost/approval/permission_scope/risk 等给中间件用)。
  - 执行层守卫: 写入类工具执行前用共享 SENSITIVE_PATTERN 拦敏感输入(命中即 raise, 不落地、不回显)。
  - memory.add 特殊: 强制经 MemoryPolicy 网关(_memory_add), 模型不能绕过阈值/敏感检查直接写长期记忆。
  - calculator 用 ast 白名单求值, 不用 eval(不给任意代码执行)。

调用关系图:
  Agent ─▶ MiddlewarePipeline.execute_tool ─▶ ToolRegistry.execute(name, input)
                                                ├─ guard_sensitive: _contains_sensitive(SENSITIVE_PATTERN)
                                                └─ ToolSpec.execute(...) ─▶ MemoryStore.add_* / safe_eval / _memory_add
  ToolRegistry.list() ─▶ Planner(可用工具清单 + inputSchema 真相源)
  _memory_add ─▶ MemoryPolicy.evaluate(记忆网关)
"""

from __future__ import annotations

import ast
import operator
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from agentic_core.runtime.contracts import MemoryPolicy
from agentic_core.memory.store import MemoryStore
from agentic_core.policies.memory import SENSITIVE_PATTERN


SideEffect = Literal["read", "write"]
RiskLevel = Literal["low", "medium", "high"]


@dataclass
class ToolSpec:
    """一个工具的类型化定义(以前是松散 dict)。

    生产级工具注册表不能只知道“函数怎么调”,还要知道:
    权限 scope、超时、成本、副作用、是否需要审批。
    当前阶段先把这些元数据结构化暴露出来,后续 Middleware/Safety 可以直接消费。
    """

    name: str
    description: str
    execute: Callable[[dict[str, Any]], Any]
    input_schema: dict[str, dict[str, Any]]
    side_effect: SideEffect
    guard_sensitive: bool = False
    permission_scope: str = "tool:read"
    timeout_ms: int = 1000
    cost_units: int = 1
    retry_count: int = 0
    risk_level: RiskLevel = "low"
    requires_approval: bool = False
    version: str = "1.0"

    def to_public_dict(self) -> dict[str, Any]:
        """返回给 Planner/审计系统看的工具元数据。"""

        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
            "sideEffect": self.side_effect,
            "permissionScope": self.permission_scope,
            "timeoutMs": self.timeout_ms,
            "costUnits": self.cost_units,
            "retryCount": self.retry_count,
            "riskLevel": self.risk_level,
            "requiresApproval": self.requires_approval,
            "guardSensitive": self.guard_sensitive,
            "version": self.version,
        }


class ToolRegistry:
    """工具注册表。

    在 agentic 应用里,LLM 不应该直接执行真实操作。
    正确做法是:

        LLM/Planner 只输出: 我要调用哪个工具 + 参数
        ToolRegistry 负责: 找到工具 + 校验是否存在 + 执行函数

    这样可以把“思考”和“行动”隔离开,系统更可控。
    """

    def __init__(self, memory: MemoryStore, memory_policy: MemoryPolicy) -> None:
        # memory 注入进来,是因为 note.add / todo.add / memory.add 这些工具需要写记忆。
        self.memory = memory

        # memory_policy 注入进来,是因为 memory.add 工具必须走一遍记忆策略,
        # 不能让模型绕过阈值和敏感信息检查直接写长期记忆。
        self.memory_policy = memory_policy

        # _tools: key 是工具名,value 是类型化的 ToolSpec。
        self._tools: dict[str, ToolSpec] = {}

        # 初始化时注册默认工具。
        self._register_defaults()

    def list(self) -> list[dict[str, Any]]:
        """返回给 Planner 看的工具清单。

        只暴露 name / description / inputSchema,不暴露 Python 函数本身。
        inputSchema 是本工具参数的唯一真相源: planner 的 prompt 提示和参数校验都从这里派生。
        """
        return [spec.to_public_dict() for spec in self._tools.values()]

    def has(self, name: str) -> bool:
        """判断工具是否存在。"""
        return name in self._tools

    def get_spec(self, name: str) -> ToolSpec | None:
        """读取工具定义,供 middleware/safety 使用。"""

        return self._tools.get(name)

    def execute(self, name: str, input_data: dict[str, Any] | None = None) -> Any:
        """执行一个工具。

        参数:
            name: 工具名,例如 "calculator"
            input_data: 工具参数,例如 {"expression": "128 * 7"}

        如果工具不存在,抛出 ValueError。Agent 会捕获错误并变成 observation。
        """
        tool = self._tools.get(name)
        if not tool:
            raise ValueError(f"unknown tool: {name}")
        data = input_data or {}
        # 写入类工具在执行前统一拦截敏感信息。这是所有工具调用的唯一入口,
        # 不管 planner 是规则还是 LLM 都绕不过——敏感信息不落地,不依赖走的是哪条路。
        if tool.guard_sensitive and _contains_sensitive(data):
            raise ValueError("拒绝写入敏感信息(密码/密钥/证件号等),不落地。")
        return tool.execute(data)

    def _register(
        self,
        name: str,
        description: str,
        execute: Callable[[dict[str, Any]], Any],
        input_schema: dict[str, dict[str, Any]] | None = None,
        side_effect: SideEffect = "read",
        guard_sensitive: bool = False,
        permission_scope: str | None = None,
        timeout_ms: int = 1000,
        cost_units: int = 1,
        retry_count: int = 0,
        risk_level: RiskLevel = "low",
        requires_approval: bool = False,
        version: str = "1.0",
    ) -> None:
        """注册一个工具。

        input_schema 描述工具参数,格式为 字段名 -> spec,例如:
            {"expression": {"type": "string", "required": True}}
        这是该工具参数的唯一真相源,无参工具(如 todo.list)传空 dict。
        其他字段是生产级治理元数据,当前先暴露,后续给 middleware/safety 消费。
        """
        self._tools[name] = ToolSpec(
            name=name,
            description=description,
            execute=execute,
            input_schema=input_schema or {},
            side_effect=side_effect,
            guard_sensitive=guard_sensitive,
            permission_scope=permission_scope or _default_permission_scope(name, side_effect),
            timeout_ms=timeout_ms,
            cost_units=cost_units,
            retry_count=max(0, retry_count),
            risk_level=risk_level,
            requires_approval=requires_approval,
            version=version,
        )

    def _register_defaults(self) -> None:
        """注册本项目内置的几个教学工具。"""
        self._register(
            "calculator",
            "Evaluate a basic arithmetic expression.",
            # lambda 是匿名函数。
            # 这里等价于定义一个普通函数:
            # def execute_calculator(input_data): ...
            lambda input_data: {
                "expression": str(input_data["expression"]),
                "result": safe_eval_arithmetic(str(input_data["expression"])),
            },
            {"expression": {"type": "string", "required": True}},
            permission_scope="tool:calculator:read",
            timeout_ms=500,
            cost_units=1,
        )
        self._register(
            "note.add",
            "Persist a learning note into memory.",
            # input_data["text"] 如果不存在会抛 KeyError,
            # Agent 会捕获并记录为失败 observation。
            lambda input_data: self.memory.add_note(str(input_data["text"])).to_dict(),
            {"text": {"type": "string", "required": True}},
            side_effect="write",
            guard_sensitive=True,
            permission_scope="memory:note:write",
            timeout_ms=1000,
            cost_units=2,
            risk_level="medium",
        )
        self._register(
            "todo.add",
            "Add a todo item.",
            lambda input_data: self.memory.add_todo(str(input_data["text"])).to_dict(),
            {"text": {"type": "string", "required": True}},
            side_effect="write",
            guard_sensitive=True,
            permission_scope="memory:todo:write",
            timeout_ms=1000,
            cost_units=2,
            risk_level="medium",
        )
        self._register(
            "todo.list",
            "List all todo items.",
            lambda _input_data: [todo.to_dict() for todo in self.memory.list_todos()],
            permission_scope="memory:todo:read",
            timeout_ms=500,
        )
        self._register(
            "study.plan",
            "Create a focused study plan for a topic, respecting a max_minutes limit.",
            self._study_plan,
            {
                "topic": {"type": "string", "required": True},
                "max_minutes": {"type": "integer", "required": False},
            },
            permission_scope="study:plan:read",
            timeout_ms=1000,
            cost_units=1,
        )
        self._register(
            "memory.add",
            "Propose a long-term memory. Gated by the memory policy; low-value or sensitive text is rejected.",
            self._memory_add,
            {"text": {"type": "string", "required": True}},
            side_effect="write",
            permission_scope="memory:long_term:write",
            timeout_ms=1000,
            cost_units=2,
            risk_level="medium",
        )

    def _memory_add(self, input_data: dict[str, Any]) -> dict[str, Any]:
        """网关化的 memory.add: 模型只能提议 text,是否保存由 MemoryPolicy 决定。

        模型传入的 type / reason / scores 被忽略,统一由 policy 派生。
        这样长期记忆只有一道闸: 敏感信息、低价值文本都会在这里被拦下,
        和 Agent 层对用户输入的策略评估保持一致。

        被拒不是失败,而是正常业务结果,所以返回结构化结果而不是抛异常。
        """
        text = str(input_data["text"])
        decision = self.memory_policy.evaluate(text)
        if not decision.save:
            return {
                "saved": False,
                "reason": decision.reason,
                "scores": decision.scores,
            }
        memory = self.memory.add_long_term_memory(
            memory_type=decision.memory_type,
            text=decision.text,
            reason=decision.reason,
            scores=decision.scores,
        )
        return {"saved": True, "memory": memory.to_dict()}

    def _study_plan(self, input_data: dict[str, Any]) -> dict[str, Any]:
        """生成一个紧凑学习计划。

        工具只做确定性计划生成,不读取用户原话。
        max_minutes 由 planner 从当前目标或长期记忆里提取后传入。
        """

        topic = str(input_data["topic"]).strip()
        max_minutes = _coerce_positive_int(input_data.get("max_minutes"), default=45)
        step_minutes = max(5, min(15, max_minutes // 3 or max_minutes))
        remaining = max_minutes
        templates = [
            f"梳理 {topic} 的核心概念",
            f"运行一个 {topic} 相关的小实验",
            f"复盘结果并记录一个可改进点",
        ]
        steps: list[str] = []
        for index, title in enumerate(templates):
            if remaining <= 0:
                break
            minutes = min(step_minutes, remaining)
            if index == len(templates) - 1:
                minutes = remaining
            steps.append(f"{minutes} 分钟: {title}")
            remaining -= minutes
        return {
            "topic": topic,
            "maxMinutes": max_minutes,
            "steps": steps,
        }


def _contains_sensitive(input_data: dict[str, Any]) -> bool:
    """输入里任一字符串值命中敏感模式就算敏感。复用 MemoryPolicy 的同一份 SENSITIVE_PATTERN。"""
    return any(
        isinstance(value, str) and SENSITIVE_PATTERN.search(value)
        for value in input_data.values()
    )


def _coerce_positive_int(value: Any, default: int) -> int:
    """把工具输入安全转成正整数。"""

    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def _default_permission_scope(name: str, side_effect: SideEffect) -> str:
    operation = "write" if side_effect == "write" else "read"
    return f"tool:{name}:{operation}"


def safe_eval_arithmetic(expression: str) -> int | float:
    """安全计算基础算术表达式。

    不直接使用 eval("128 * 7"),因为 eval 可以执行危险代码。
    这里用 ast.parse 把表达式解析成语法树,再只允许数字和基础运算符。
    """
    node = ast.parse(expression, mode="eval")
    return _eval_node(node.body)


def _eval_node(node: ast.AST) -> int | float:
    """递归计算 AST 节点。

    递归的意思是: 函数在处理一个大表达式时,会继续调用自己处理子表达式。

    例如 128 * 7 的 AST 大概是:
        BinOp(left=128, op=Mult, right=7)
    """
    binary_ops: dict[type, Callable[[Any, Any], Any]] = {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.Mult: operator.mul,
        ast.Div: operator.truediv,
        ast.Mod: operator.mod,
    }
    unary_ops: dict[type, Callable[[Any], Any]] = {ast.UAdd: operator.pos, ast.USub: operator.neg}

    # 数字常量,例如 128 或 7。
    if isinstance(node, ast.Constant) and isinstance(node.value, int | float):
        return node.value

    # 二元运算,例如 128 * 7、10 + 3。
    if isinstance(node, ast.BinOp) and type(node.op) in binary_ops:
        return binary_ops[type(node.op)](_eval_node(node.left), _eval_node(node.right))

    # 一元运算,例如 -5。
    if isinstance(node, ast.UnaryOp) and type(node.op) in unary_ops:
        return unary_ops[type(node.op)](_eval_node(node.operand))

    # 其他语法都拒绝,比如函数调用、变量、属性访问。
    raise ValueError("calculator only accepts basic arithmetic expressions")
