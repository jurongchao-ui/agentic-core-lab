export class Agent {
  constructor({ planner, tools, memory, maxSteps = 8 }) {
    this.planner = planner;
    this.tools = tools;
    this.memory = memory;
    this.maxSteps = maxSteps;
  }

  async run(goal) {
    const runId = `run_${Date.now()}`;
    const trace = [];

    for (let step = 1; step <= this.maxSteps; step += 1) {
      const context = {
        runId,
        goal,
        step,
        trace,
        memory: this.memory,
        availableTools: this.tools.list(),
      };

      const action = this.planner.next(context);

      if (action.type === "final") {
        this.memory.recordEvent({ runId, type: "final", answer: action.answer });
        return {
          runId,
          answer: action.answer,
          trace,
          memory: this.memory.snapshot(),
        };
      }

      const startedAt = Date.now();
      const observation = await this.executeAction(action, startedAt);
      trace.push({ step, action, observation });
      this.memory.recordEvent({ runId, step, action, observation });
    }

    return {
      runId,
      answer: `达到最大步数 ${this.maxSteps},任务未能自动完成。`,
      trace,
      memory: this.memory.snapshot(),
    };
  }

  async executeAction(action, startedAt) {
    try {
      const output = await this.tools.execute(action.toolName, action.input);
      return {
        ok: true,
        output,
        elapsedMs: Date.now() - startedAt,
      };
    } catch (error) {
      return {
        ok: false,
        error: error instanceof Error ? error.message : String(error),
        elapsedMs: Date.now() - startedAt,
      };
    }
  }
}
