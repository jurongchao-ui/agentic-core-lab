from __future__ import annotations

import json
import re
from typing import Any

from .contracts import LlmClient, PlannerContext
from .json_utils import extract_json_object
from .schemas import Action


class RuleBasedPlanner:
    """不用 LLM 的规则型 planner。

    它的作用有两个:
    1. 作为教学版本,让你看到“不靠模型也能跑通 agent loop”。
    2. 作为 HermesPlanner 的兜底方案,当模型输出不合法时接管任务。
    """

    def next(self, context: PlannerContext) -> Action:
        # context 是 Agent 传进来的上下文包。
        # 这里取出最常用的三个字段。
        goal = context["goal"]
        trace = context["trace"]
        memory = context["memory"]

        # 从用户输入中提取几个意图信号。
        # extract_arithmetic("帮我计算 128 * 7") -> "128 * 7"
        expression = extract_arithmetic(goal)
        todo_text = extract_todo(goal)
        should_record_note = bool(re.search(r"记录|笔记|note", goal, re.I))
        should_list_todos = bool(re.search(r"列出待办|查看待办|list todo", goal, re.I))

        if expression and not has_tool(trace, "calculator"):
            # 如果用户目标里有算式,并且还没调用过 calculator,下一步就调用 calculator。
            return Action.tool(
                "calculator",
                {"expression": expression},
                "目标中包含算术表达式,先调用 calculator 获得确定结果。",
                source="rule",
            )

        if todo_text and not has_tool(trace, "todo.add"):
            # 如果用户要求添加待办,并且还没添加过,下一步调用 todo.add。
            return Action.tool(
                "todo.add",
                {"text": todo_text},
                "用户要求添加待办,需要写入待办记忆。",
                source="rule",
            )

        if (
            expression
            and should_record_note
            and has_failed_tool(trace, "calculator")
            and not has_successful_tool(trace, "calculator")
        ):
            # 计算失败时,不要继续把“失败的计算任务”写成成功笔记。
            # 依赖关系应该由 observation 决定: 没有成功计算结果,就不能记录“计算学习笔记”。
            return Action.final(
                build_answer(goal, trace, memory.snapshot()),
                "计算失败,依赖计算结果的笔记不能继续写入。",
                source="rule",
            )

        if should_record_note and not has_tool(trace, "note.add"):
            # 如果用户要求记录笔记,优先把前一步计算结果整理成笔记。
            calc = last_successful_tool(trace, "calculator")
            text = (
                f"计算 {calc['observation']['output']['expression']} = {calc['observation']['output']['result']}"
                if calc
                else f"学习笔记: {goal}"
            )
            return Action.tool(
                "note.add",
                {"text": text},
                "用户要求记录为笔记,需要写入学习笔记。",
                source="rule",
            )

        if should_list_todos and not has_tool(trace, "todo.list"):
            # 如果用户要求查看待办,下一步调用 todo.list。
            return Action.tool(
                "todo.list",
                {},
                "用户要求列出待办,需要读取待办列表。",
                source="rule",
            )

        # 没有更多工具要调用时,生成最终回答。
        return Action.final(
            build_answer(goal, trace, memory.snapshot()),
            "已完成所需工具调用,可以汇总结果。",
            source="rule",
        )


class HermesPlanner:
    """使用 Ollama/Hermes 的 LLM Planner。

    注意:
        LLM Planner 只负责“提出下一步 action”。
        它提出的 action 必须经过程序校验,不能直接相信。

    为什么?
        本地模型可能会:
        - 输出非 JSON
        - 选一个不存在的工具
        - 调 calculator 但忘记传 expression
        - 任务还没完成就 final

    所以 HermesPlanner 内部有 fallback:
        Hermes 输出不合格 -> 抛异常 -> RuleBasedPlanner 接管。
    """

    def __init__(self, client: LlmClient, fallback: RuleBasedPlanner | None = None) -> None:
        # client 负责真正请求 Ollama。
        self.client = client

        # fallback 是兜底 planner。如果不传,默认创建一个 RuleBasedPlanner。
        self.fallback = fallback or RuleBasedPlanner()

    def next(self, context: PlannerContext) -> Action:
        """让 Hermes 规划下一步 action。

        成功路径:
            context -> prompt messages -> Ollama -> JSON -> Action

        失败路径:
            任何一步出错 -> RuleBasedPlanner.next(context)
        """
        # 先把 content 置空,这样即使解析失败,回退时也能把模型原文带进 metadata。
        content: str | None = None
        try:
            # 1. 把 context 转成 messages,发给 Ollama。
            raw = self.client.chat(self._messages(context))

            # 2. Ollama /api/chat 返回的文本在 message.content 里。
            content = raw.get("message", {}).get("content", "")

            # 3. 把模型文本解析并校验成 Action。
            action = self._parse_action(content, context)
            action.source = "hermes"
            action.metadata = {"source": "hermes", "rawModelOutput": content}
            return action
        except Exception as error:
            # 任何模型问题都不让程序崩溃,而是回退规则 planner。
            # 这就是“模型不可靠,系统要可靠”的工程边界。
            action = self.fallback.next(context)
            action.reason = f"Hermes planner fallback: {error}. {action.reason}"
            action.source = "rule_fallback"
            action.metadata = {
                "source": "rule_fallback",
                "rawModelOutput": content,
                "error": str(error),
            }
            return action

    def _messages(self, context: PlannerContext) -> list[dict[str, str]]:
        """构造发给 Hermes 的 prompt。

        Ollama chat API 的 messages 结构类似:
            [{"role": "system", "content": "..."}, {"role": "user", "content": "..."}]

        system message 定规则,user message 放当前任务上下文。
        """

        # payload 是模型能看到的“世界状态”。
        # 注意我们没有把 Python 对象直接发给模型,而是转成 JSON 字符串。
        payload = {
            "goal": context["goal"],
            "step": context["step"],
            "trace": context["trace"],
            "memory": context["memory"].snapshot(),
            "availableTools": context["available_tools"],
            "toolInputSchemas": {
                tool["name"]: {
                    field: describe_input_field(spec)
                    for field, spec in tool.get("inputSchema", {}).items()
                }
                for tool in context["available_tools"]
            },
        }
        return [
            {
                "role": "system",
                "content": (
                    # 这里强制模型只输出 JSON。
                    # 但“强制”只是提示词层面的,程序仍然要在 _parse_action 里校验。
                    "You are a planner inside an agentic application. "
                    "Return only valid JSON, no markdown. "
                    "Choose exactly one action. "
                    "Tool action schema: {\"type\":\"tool\",\"toolName\":\"calculator\",\"input\":{},\"reason\":\"...\"}. "
                    "Final action schema: {\"type\":\"final\",\"answer\":\"...\",\"reason\":\"...\"}. "
                    "Use only available tool names. "
                    "memory.add is gated by a memory policy and may reject low-value or sensitive text."
                ),
            },
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]

    def _parse_action(self, content: str, context: PlannerContext) -> Action:
        """把模型输出解析成 Action,并做严格校验。

        初学者可以把这里理解为“海关”:
        模型说它要做什么,但必须先通过格式、工具名、参数、完成度检查。
        """

        # 模型有时会在 JSON 外面加解释文字,extract_json_object 会尽量取出 JSON 部分。
        data = json.loads(extract_json_object(content))
        action_type = data.get("type")
        reason = str(data.get("reason", ""))
        if action_type == "final":
            # 如果之前有工具失败,不允许模型直接 final。
            # 正确做法是继续规划或让 fallback 接管。
            failed = [item for item in context["trace"] if not item.get("observation", {}).get("ok")]
            if failed:
                raise ValueError("model tried to finalize after a failed observation")

            answer = data.get("answer")
            if not isinstance(answer, str) or not answer.strip():
                raise ValueError("final action requires non-empty answer")

            # 即使 answer 不为空,也要检查“任务是否真的完成”。
            validate_final_action(context, answer)
            return Action.final(answer, reason)
        if action_type == "tool":
            tool_name = data.get("toolName") or data.get("tool_name")
            if not isinstance(tool_name, str):
                raise ValueError("tool action requires toolName")

            # 模型只能从 available_tools 里选工具。
            allowed_tools = {tool["name"] for tool in context["available_tools"]}
            if tool_name not in allowed_tools:
                raise ValueError(f"unknown model-selected tool: {tool_name}")

            input_data = data.get("input", {})
            if not isinstance(input_data, dict):
                raise ValueError("tool input must be an object")

            # 检查工具参数是否齐全。
            # 例如 calculator 必须有 {"expression": "..."}。
            validate_tool_input(tool_name, input_data, context["available_tools"])
            return Action.tool(tool_name, input_data, reason)
        raise ValueError(f"unknown action type: {action_type}")


def describe_input_field(spec: dict[str, Any]) -> str:
    """把结构化 spec 渲染成给模型看的字符串,例如 "string, required"。"""
    requiredness = "required" if spec.get("required") else "optional"
    return f"{spec.get('type', 'string')}, {requiredness}"


def validate_tool_input(
    tool_name: str, input_data: dict[str, Any], available_tools: list[dict[str, Any]]
) -> None:
    """检查工具调用参数。

    这是 agentic 系统很核心的安全边界:
        Planner 只能提出“我要调用哪个工具,传什么参数”。
        程序必须检查参数合法后才能真的执行工具。

    参数 schema 不再硬编码在这里,而是从 available_tools(即 ToolRegistry.list())派生,
    保证 prompt 提示、校验、工具真实需求同一个真相源,不会漂移。
    """
    schema: dict[str, Any] = next(
        (tool.get("inputSchema", {}) for tool in available_tools if tool["name"] == tool_name),
        {},
    )
    for field, spec in schema.items():
        if not spec.get("required"):
            continue
        value = input_data.get(field)
        if spec.get("type", "string") == "string" and (
            not isinstance(value, str) or not value.strip()
        ):
            raise ValueError(f"{tool_name} requires input.{field}")


def validate_final_action(context: PlannerContext, answer: str) -> None:
    """检查模型是否过早结束。

    举例:
        用户说“计算 128 * 7, 然后记录成学习笔记”
        如果模型只算出了 896 就 final,其实任务没有完成。

    所以这里会检查:
        - 有算式时,calculator 是否成功执行过
        - 要求记录笔记时,note.add 是否成功执行过
        - 多步骤任务的最终回答是否足够完整
    """
    goal = context["goal"]
    trace = context["trace"]
    multi_step_goal = False
    if extract_arithmetic(goal) and not has_successful_tool(trace, "calculator"):
        raise ValueError("final action before calculator completed")
    if re.search(r"记录|笔记|note", goal, re.I):
        multi_step_goal = True
        if not has_successful_tool(trace, "note.add"):
            raise ValueError("final action before note.add completed")
        if not re.search(r"笔记|保存|记录", answer):
            raise ValueError("final answer does not mention the saved note")
    if extract_todo(goal):
        multi_step_goal = True
        if not has_successful_tool(trace, "todo.add"):
            raise ValueError("final action before todo.add completed")
    if re.search(r"列出待办|查看待办|list todo", goal, re.I):
        multi_step_goal = True
        if not has_successful_tool(trace, "todo.list"):
            raise ValueError("final action before todo.list completed")
    if multi_step_goal and len(answer.strip()) < 12:
        raise ValueError("final answer is too terse for a multi-step goal")


def extract_arithmetic(goal: str) -> str | None:
    """从用户目标里提取简单算术表达式。"""
    match = re.search(r"(\d+(?:\s*[+\-*/%]\s*\d+)+)", goal)
    return match.group(1) if match else None


def extract_todo(goal: str) -> str | None:
    """从用户目标里提取待办文本。"""
    match = re.search(r"(?:添加待办|新增待办|todo)[:：]?\s*(.+?)(?:,|，|然后|$)", goal, re.I)
    return match.group(1).strip() if match else None


def has_tool(trace: list[dict[str, Any]], tool_name: str) -> bool:
    """判断某个工具是否已经被调用过,不关心成功还是失败。"""
    return any(item.get("action", {}).get("toolName") == tool_name for item in trace)


def has_successful_tool(trace: list[dict[str, Any]], tool_name: str) -> bool:
    """判断某个工具是否已经成功执行过。"""
    return any(
        item.get("action", {}).get("toolName") == tool_name and item.get("observation", {}).get("ok")
        for item in trace
    )


def has_failed_tool(trace: list[dict[str, Any]], tool_name: str) -> bool:
    """判断某个工具是否失败过。"""
    return any(
        item.get("action", {}).get("toolName") == tool_name
        and not item.get("observation", {}).get("ok")
        for item in trace
    )


def last_successful_tool(trace: list[dict[str, Any]], tool_name: str) -> dict[str, Any] | None:
    """从后往前找最近一次成功执行的工具结果。"""
    for item in reversed(trace):
        if item.get("action", {}).get("toolName") == tool_name and item.get("observation", {}).get("ok"):
            return item
    return None


def build_answer(goal: str, trace: list[dict[str, Any]], snapshot: dict[str, Any]) -> str:
    """根据 trace 和 memory snapshot 生成最终回答。

    RuleBasedPlanner 使用这个函数输出稳定、可解释的最终结果。
    Hermes 输出太短或不完整时,也会回退到这里。
    """
    lines = [f"目标: {goal}", "执行结果:"]
    for item in trace:
        action = item["action"]
        observation = item["observation"]
        tool_name = action.get("toolName")
        if not observation.get("ok"):
            lines.append(f"- {tool_name} 失败: {observation.get('error')}")
            continue
        output = observation.get("output")
        if tool_name == "calculator":
            lines.append(f"- 计算完成: {output['expression']} = {output['result']}")
        elif tool_name == "note.add":
            lines.append(f"- 笔记已保存: {output['text']}")
        elif tool_name == "todo.add":
            lines.append(f"- 待办已添加: {output['text']}")
        elif tool_name == "todo.list":
            lines.append(
                "- 当前待办: "
                + ("; ".join(f"{todo['id']}:{todo['text']}" for todo in output) if output else "当前没有待办")
            )
        elif tool_name == "memory.add":
            if output.get("saved"):
                lines.append(f"- 长期记忆已保存: {output['memory']['text']}")
            else:
                lines.append(f"- 长期记忆未保存(未通过记忆策略): {output.get('reason')}")
    lines.append(
        "记忆状态: "
        f"{len(snapshot['notes'])} 条笔记, "
        f"{len(snapshot['todos'])} 条待办, "
        f"{len(snapshot['longTermMemories'])} 条长期记忆。"
    )
    return "\n".join(lines)
