from __future__ import annotations

import ast
import operator
from collections.abc import Callable
from typing import Any

from .memory import MemoryStore
from .memory_policy import SENSITIVE_PATTERN, MemoryPolicy


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

        # _tools 是一个 dict:
        # key 是工具名,例如 "calculator"
        # value 是工具信息,包括 description 和 execute 函数。
        self._tools: dict[str, dict[str, Any]] = {}

        # 初始化时注册默认工具。
        self._register_defaults()

    def list(self) -> list[dict[str, Any]]:
        """返回给 Planner 看的工具清单。

        只暴露 name / description / inputSchema,不暴露 Python 函数本身。
        inputSchema 是本工具参数的唯一真相源: planner 的 prompt 提示和参数校验都从这里派生。
        """
        return [
            {
                "name": name,
                "description": item["description"],
                "inputSchema": item["input_schema"],
            }
            for name, item in self._tools.items()
        ]

    def has(self, name: str) -> bool:
        """判断工具是否存在。"""
        return name in self._tools

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
        if tool["guard_sensitive"] and _contains_sensitive(data):
            raise ValueError("拒绝写入敏感信息(密码/密钥/证件号等),不落地。")
        return tool["execute"](data)

    def _register(
        self,
        name: str,
        description: str,
        execute: Callable[[dict[str, Any]], Any],
        input_schema: dict[str, dict[str, Any]] | None = None,
        guard_sensitive: bool = False,
    ) -> None:
        """注册一个工具。

        execute 是一个函数,接收 dict 参数,返回任意结果。
        Callable[[dict[str, Any]], Any] 的意思是:
            这是一个可调用对象,输入是 dict[str, Any],输出可以是任意类型。

        input_schema 描述工具参数,格式为 字段名 -> spec,例如:
            {"expression": {"type": "string", "required": True}}
        这是该工具参数的唯一真相源,无参工具(如 todo.list)传空 dict。
        """
        self._tools[name] = {
            "description": description,
            "execute": execute,
            "input_schema": input_schema or {},
            "guard_sensitive": guard_sensitive,
        }

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
        )
        self._register(
            "note.add",
            "Persist a learning note into memory.",
            # input_data["text"] 如果不存在会抛 KeyError,
            # Agent 会捕获并记录为失败 observation。
            lambda input_data: self.memory.add_note(str(input_data["text"])),
            {"text": {"type": "string", "required": True}},
            guard_sensitive=True,
        )
        self._register(
            "todo.add",
            "Add a todo item.",
            lambda input_data: self.memory.add_todo(str(input_data["text"])),
            {"text": {"type": "string", "required": True}},
            guard_sensitive=True,
        )
        self._register(
            "todo.list",
            "List all todo items.",
            lambda _input_data: self.memory.list_todos(),
        )
        self._register(
            "memory.add",
            "Propose a long-term memory. Gated by the memory policy; low-value or sensitive text is rejected.",
            self._memory_add,
            {"text": {"type": "string", "required": True}},
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
        return {"saved": True, "memory": memory}


def _contains_sensitive(input_data: dict[str, Any]) -> bool:
    """输入里任一字符串值命中敏感模式就算敏感。复用 MemoryPolicy 的同一份 SENSITIVE_PATTERN。"""
    return any(
        isinstance(value, str) and SENSITIVE_PATTERN.search(value)
        for value in input_data.values()
    )


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
    binary_ops = {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.Mult: operator.mul,
        ast.Div: operator.truediv,
        ast.Mod: operator.mod,
    }
    unary_ops = {ast.UAdd: operator.pos, ast.USub: operator.neg}

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
