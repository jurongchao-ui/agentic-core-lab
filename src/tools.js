function evaluateArithmetic(expression) {
  if (!/^[\d\s+\-*/().%]+$/.test(expression)) {
    throw new Error("calculator only accepts arithmetic expressions");
  }

  // The expression is validated to arithmetic characters before evaluation.
  return Function(`"use strict"; return (${expression});`)();
}

export function createToolRegistry(memory) {
  const tools = new Map();

  function register(tool) {
    tools.set(tool.name, tool);
  }

  register({
    name: "calculator",
    description: "Evaluate a basic arithmetic expression.",
    async execute(input) {
      if (!input?.expression) {
        throw new Error("calculator requires input.expression");
      }
      return {
        expression: input.expression,
        result: evaluateArithmetic(String(input.expression)),
      };
    },
  });

  register({
    name: "note.add",
    description: "Persist a learning note into long-term memory.",
    async execute(input) {
      if (!input?.text) {
        throw new Error("note.add requires input.text");
      }
      return memory.addNote(String(input.text));
    },
  });

  register({
    name: "todo.add",
    description: "Add a todo item.",
    async execute(input) {
      if (!input?.text) {
        throw new Error("todo.add requires input.text");
      }
      return memory.addTodo(String(input.text));
    },
  });

  register({
    name: "todo.list",
    description: "List all todo items.",
    async execute() {
      return memory.listTodos();
    },
  });

  return {
    list() {
      return [...tools.values()].map(({ name, description }) => ({
        name,
        description,
      }));
    },

    async execute(name, input) {
      const tool = tools.get(name);
      if (!tool) {
        throw new Error(`unknown tool: ${name}`);
      }
      return tool.execute(input ?? {});
    },
  };
}
