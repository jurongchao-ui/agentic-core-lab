function extractArithmetic(goal) {
  const match = goal.match(/(\d+(?:\s*[+\-*/%]\s*\d+)+)/);
  return match?.[1] ?? null;
}

function extractTodo(goal) {
  const match = goal.match(/(?:添加待办|新增待办|todo)[:：]?\s*(.+?)(?:,|，|然后|$)/i);
  return match?.[1]?.trim() ?? null;
}

function hasTool(trace, toolName) {
  return trace.some((item) => item.action?.type === "tool" && item.action.toolName === toolName);
}

function lastSuccessfulTool(trace, toolName) {
  return [...trace]
    .reverse()
    .find((item) => item.action?.toolName === toolName && item.observation?.ok);
}

export class RuleBasedPlanner {
  next(context) {
    const { goal, trace, memory } = context;
    const expression = extractArithmetic(goal);
    const todoText = extractTodo(goal);
    const shouldRecordNote = /记录|笔记|note/i.test(goal);
    const shouldListTodos = /列出待办|查看待办|list todo/i.test(goal);

    if (expression && !hasTool(trace, "calculator")) {
      return {
        type: "tool",
        toolName: "calculator",
        input: { expression },
        reason: "目标中包含算术表达式,先调用 calculator 获得确定结果。",
      };
    }

    if (todoText && !hasTool(trace, "todo.add")) {
      return {
        type: "tool",
        toolName: "todo.add",
        input: { text: todoText },
        reason: "用户要求添加待办,需要写入待办记忆。",
      };
    }

    if (shouldRecordNote && !hasTool(trace, "note.add")) {
      const calc = lastSuccessfulTool(trace, "calculator");
      const text = calc
        ? `计算 ${calc.observation.output.expression} = ${calc.observation.output.result}`
        : `学习笔记: ${goal}`;

      return {
        type: "tool",
        toolName: "note.add",
        input: { text },
        reason: "用户要求记录为笔记,需要写入长期记忆。",
      };
    }

    if (shouldListTodos && !hasTool(trace, "todo.list")) {
      return {
        type: "tool",
        toolName: "todo.list",
        input: {},
        reason: "用户要求列出待办,需要读取待办列表。",
      };
    }

    return {
      type: "final",
      answer: buildAnswer(goal, trace, memory),
      reason: "已完成所需工具调用,可以汇总结果。",
    };
  }
}

function buildAnswer(goal, trace, memory) {
  const lines = [`目标: ${goal}`, "执行结果:"];

  for (const item of trace) {
    if (!item.observation?.ok) {
      lines.push(`- ${item.action.toolName} 失败: ${item.observation?.error}`);
      continue;
    }

    if (item.action.toolName === "calculator") {
      lines.push(
        `- 计算完成: ${item.observation.output.expression} = ${item.observation.output.result}`
      );
    }

    if (item.action.toolName === "note.add") {
      lines.push(`- 笔记已保存: ${item.observation.output.text}`);
    }

    if (item.action.toolName === "todo.add") {
      lines.push(`- 待办已添加: ${item.observation.output.text}`);
    }

    if (item.action.toolName === "todo.list") {
      const todos = item.observation.output;
      lines.push(
        todos.length
          ? `- 当前待办: ${todos.map((todo) => `${todo.id}:${todo.text}`).join("; ")}`
          : "- 当前没有待办"
      );
    }
  }

  const snapshot = memory.snapshot();
  lines.push(`记忆状态: ${snapshot.notes.length} 条笔记, ${snapshot.todos.length} 条待办。`);

  return lines.join("\n");
}
