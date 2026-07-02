#!/usr/bin/env node
import { Agent } from "./agent.js";
import { MemoryStore } from "./memory.js";
import { RuleBasedPlanner } from "./planner.js";
import { createToolRegistry } from "./tools.js";

const goal = process.argv.slice(2).join(" ").trim();

if (!goal) {
  console.log("Usage: node src/index.js \"帮我计算 128 * 7, 然后把结果记录成学习笔记\"");
  process.exit(1);
}

const memory = new MemoryStore();
const tools = createToolRegistry(memory);
const planner = new RuleBasedPlanner();
const agent = new Agent({ planner, tools, memory });

const result = await agent.run(goal);

console.log("\n=== Final Answer ===");
console.log(result.answer);

console.log("\n=== Trace ===");
for (const item of result.trace) {
  console.log(
    JSON.stringify(
      {
        step: item.step,
        reason: item.action.reason,
        action: {
          type: item.action.type,
          toolName: item.action.toolName,
          input: item.action.input,
        },
        observation: item.observation,
      },
      null,
      2
    )
  );
}

console.log("\n=== Memory Snapshot ===");
console.log(JSON.stringify(result.memory, null, 2));
