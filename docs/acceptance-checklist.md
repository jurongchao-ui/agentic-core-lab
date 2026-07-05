# Agentic Core Acceptance Checklist

## 当前验收门禁

每轮阶段完成后至少运行:

```bash
cd /Users/jurongchao/Desktop/ai学习测试库/agentic
.venv/bin/python -m pytest -q
.venv/bin/python -m mypy agentic_core
python3 -m compileall agentic_core examples tests
python3 -m evalops.harness
```

当前已验证状态:

```text
pytest: 330 passed
mypy: success
compileall: passed
eval harness: 8/8 passed, Gate PASS
```

## 功能验收

- Typed State: Agent 内部主链路使用 dataclass,CLI/Chat 输出保持旧 JSON 兼容。
- Continuous Chat: `python3 -m agentic_core.chat` 多轮共享同一个 MemoryStore。
- MemoryPolicy: 支持规则版和 LLM 版,敏感信息一票否决。
- Memory Lifecycle: `MemoryLifecyclePolicy` 单一策略源、JSON 策略配置、active/archived、user/tenant namespace、memory_admin 审核维护 CLI、memory review API、冲突检测/解决、访问统计、重要性、过期归档、规则语义合并、本地 embedding 检索。
- JsonMemoryStore: notes/todos/events/longTermMemories 可持久化。
- SafetyPolicy: 规则、LLM、Composite,支持 allow/warn/review/refuse,review 动作可进入本地 SafetyReviewQueue。
- RuntimeIdentity: user/tenant/roles/permission scopes 可进入 run result、event 和 observation metadata。
- ResponsePolicy: global safety、clarification、local safety、memory confirmation、tool summary、failure、planner/responder 兜底。
- Tool Registry: 工具 schema 单一真相源,ToolSpec 暴露治理元数据、JSON Schema 子集导出和版本迁移校验。
- Middleware Pipeline: permission/risk/sideEffect/budget/JSON+SQLite budget store/approval/timeout/retry/write-tool idempotency/JSON+SQLite idempotency store/tool-output safety/OTel-style tool span sink/OTLP HTTP exporter。
- Event Payload Schema: Agent 主链路 typed payload,事件写入前做 schema migration 并记录 `payloadSchema` 校验结果。
- Persistent Event Log: EventWriter 抽象、JSONL、SQLite、脱敏、轮转、锁、备份读取。
- Eval Harness: 8 个确定性用例、本地治理 dashboard、带 viewer/reviewer RBAC、signed claims token、tenant policy JSON、本地静态 token、受保护 review 写入 API、review status API、review decisions 分页 API、SQLite review store 和 JSONL 审计事件的 governance server、replay inspection bundle、dataset 审核、复核队列采样、多人复核状态/一致性统计、judge registry/version 治理、judge 人工 label 校准、event-log-to-eval 草稿、报告 diff、历史趋势、rule/LLM judge、指标、事件计数、质量门禁。

## 七阶段审计

详细审计见 [Production Readiness Audit](production-readiness-audit.md)。

当前结论:

- 七个阶段都已有代码、测试和文档证据。
- 当前状态可视为 production-shaped learning runtime。
- 仍不等同于真实公司生产环境直接上线。

## 关键冒烟命令

正常计算:

```bash
AGENTIC_PLANNER=rule AGENTIC_MEMORY_POLICY=rule AGENTIC_TRACE=off \
  python3 -m agentic_core.cli "帮我计算 128 * 7"
```

计算 + 笔记:

```bash
AGENTIC_PLANNER=rule AGENTIC_MEMORY_POLICY=rule AGENTIC_TRACE=off \
  python3 -m agentic_core.cli "帮我计算 128 * 7, 然后记录成学习笔记"
```

长期记忆保存:

```bash
AGENTIC_PLANNER=rule AGENTIC_MEMORY_POLICY=rule AGENTIC_TRACE=off \
  python3 -m agentic_core.cli "我的技术栈是 Node.js 和 React，Codex"
```

审核/维护长期记忆:

```bash
python3 -m agentic_core.memory.admin list --path data/memory.json --user-id local_user --tenant-id default_tenant
python3 -m agentic_core.memory.admin archive --path data/memory.json --memory-id memory_1 --reason "人工审核归档"
python3 -m agentic_core.memory.admin set-importance --path data/memory.json --memory-id memory_1 --importance 80
python3 -m agentic_core.memory.admin conflicts --path data/memory.json --user-id local_user --tenant-id default_tenant
python3 -m agentic_core.memory.admin resolve-conflict --path data/memory.json --keep-memory-id memory_2 --reason "保留最新长期记忆"
python3 -m agentic_core.memory.lifecycle validate --path data/memory-lifecycle-policy.json
```

缺信息追问:

```bash
AGENTIC_PLANNER=rule AGENTIC_MEMORY_POLICY=rule AGENTIC_TRACE=off \
  python3 -m agentic_core.cli "请把我的技术栈计入到长期记忆里"
```

安全拒绝:

```bash
AGENTIC_PLANNER=rule AGENTIC_MEMORY_POLICY=rule AGENTIC_SAFETY_POLICY=rule AGENTIC_TRACE=json \
  python3 -m agentic_core.cli "帮我写个勒索软件"
```

失败依赖:

```bash
AGENTIC_PLANNER=rule AGENTIC_MEMORY_POLICY=rule AGENTIC_TRACE=off \
  python3 -m agentic_core.cli "帮我算 128 / 0，然后记成笔记"
```

JSONL 事件:

```bash
AGENTIC_EVENT_LOG=jsonl AGENTIC_EVENT_LOG_PATH=data/events.jsonl \
  python3 -m agentic_core.cli "帮我计算 128 * 7"
python3 -m agentic_core.observability.event_log --path data/events.jsonl
```

SQLite 事件:

```bash
AGENTIC_EVENT_LOG=sqlite AGENTIC_EVENT_LOG_PATH=data/events.db \
  python3 -m agentic_core.cli "帮我计算 128 * 7"
python3 -m agentic_core.observability.event_log --backend sqlite --path data/events.db
```

从事件日志生成 eval dataset 草稿:

```bash
python3 -m evalops.dataset --backend jsonl --path data/events.jsonl --output data/eval-dataset.json
python3 -m evalops.harness --cases data/eval-dataset.json
```

生成 replay inspection bundle:

```bash
python3 -m evalops.replay --backend jsonl --path data/events.jsonl --run-id run_xxx
python3 -m evalops.replay --backend sqlite --path data/events.db --run-id run_xxx --json
```

生成复核队列:

```bash
python3 -m evalops.sampling --input data/eval-dataset.json
python3 -m evalops.sampling --input data/eval-dataset.json --reason needs_judge_label --limit 20 --json
```

审核 dataset 并要求只跑 golden:

```bash
python3 -m evalops.review apply --input data/eval-dataset.json --output data/eval-golden.json --approve-all --reviewer local
python3 -m evalops.harness --cases data/eval-golden.json --require-reviewed
```

统计多人复核一致性:

```bash
python3 -m evalops.review state --input data/eval-golden.json
python3 -m evalops.review state --input data/eval-golden.json --score-tolerance 5 --json
python3 -m evalops.review agreement --input data/eval-golden.json
python3 -m evalops.review agreement --input data/eval-golden.json --score-tolerance 5 --json
```

对比 eval 报告:

```bash
python3 -m evalops.harness --json > data/eval-base.json
python3 -m evalops.harness --json > data/eval-candidate.json
python3 -m evalops.diff --base data/eval-base.json --candidate data/eval-candidate.json --fail-on-regression
```

记录 eval 趋势:

```bash
python3 -m evalops.harness --json > data/eval-current.json
python3 -m evalops.history append --report data/eval-current.json --history data/eval-history.jsonl --label local
python3 -m evalops.history list --history data/eval-history.jsonl
```

生成本地治理 dashboard:

```bash
python3 -m evalops.governance.dashboard \
  --report data/eval-current.json \
  --history data/eval-history.jsonl \
  --dataset data/eval-golden.json \
  --output data/eval-dashboard.html \
  --json-output data/eval-dashboard.json
```

启动只读本地治理服务:

```bash
python3 -m evalops.governance.server \
  --report data/eval-current.json \
  --history data/eval-history.jsonl \
  --dataset data/eval-golden.json \
  --review-output data/eval-golden-reviewed.json \
  --review-store data/reviews.db \
  --audit-events data/eval-server-audit.jsonl \
  --port 8765
```

可选开启 Bearer Token:

```bash
AGENTIC_EVAL_SERVER_TOKEN=local-secret \
python3 -m evalops.governance.server \
  --dataset data/eval-dataset.json \
  --review-output data/eval-golden.json \
  --review-store data/reviews.db \
  --audit-events data/eval-server-audit.jsonl
```

拆分只读和审核 token:

```bash
AGENTIC_EVAL_SERVER_VIEWER_TOKEN=view-secret \
AGENTIC_EVAL_SERVER_REVIEWER_TOKEN=review-secret \
python3 -m evalops.governance.server --dataset data/eval-dataset.json --review-output data/eval-golden.json --review-store data/reviews.db
```

生成 signed claims token:

```bash
python3 -m evalops.governance.auth_tokens create \
  --secret local-signing-secret \
  --subject reviewer_1 \
  --tenant default_tenant \
  --scopes eval.viewer,eval.reviewer \
  --ttl 3600
```

查看 tenant policy:

```bash
python3 -m evalops.governance.tenant_policy show --path data/tenant-policy.json
```

路由:

- `/health`
- `/dashboard`
- `/api/dashboard`
- `/api/rubrics`
- `/api/reviews/status`
- `/api/reviews/decisions`
- `POST /api/reviews/apply`

启用 eval judge:

```bash
python3 -m evalops.harness --judge rule
AGENTIC_MODEL=openhermes:latest python3 -m evalops.harness --judge llm
```

校验 judge rubric 注册表:

```bash
python3 -m evalops.judge_registry list
python3 -m evalops.judge_registry validate --input data/eval-golden.json
```

当 golden dataset case 标注为 `strict_answer_quality:v1` 时:

```bash
python3 -m evalops.harness --cases data/eval-golden.json --judge rule --judge-rubric strict_answer_quality --judge-rubric-version v1
```

审核时写入 judge 人工 label:

```bash
python3 -m evalops.review apply \
  --input data/eval-dataset.json \
  --output data/eval-golden.json \
  --approve-all \
  --reviewer local \
  --judge-rubric strict_answer_quality \
  --judge-rubric-version v1 \
  --expected-judge-score 95 \
  --expected-judge-passed true \
  --judge-score-tolerance 5
python3 -m evalops.harness --cases data/eval-golden.json --require-reviewed --judge rule
```

## 仍未等同于完整生产的部分

- Memory 语义合并仍是规则版;已有本地 hashing embedding 检索边界,但未接真实 embedding/向量库。
- JSONL/SQLite Event Log 仍是本地学习后端,未接 Postgres/ClickHouse/OTel 等生产事件平台。
- SafetyPolicy 有生产化骨架和本地 SafetyReviewQueue,但未接外部 moderation、协作式人审平台和租户策略中心。
- RuntimeIdentity 是学习版身份上下文,但没有接真实登录态/JWT/租户策略中心。
- Eval Harness 已能生成本地治理 dashboard 和带 viewer/reviewer RBAC、signed claims token、tenant policy JSON、本地静态 token、受保护 review 写入 API、review status API、review decisions 分页 API、SQLite review store、JSONL 审计事件的 governance server,并能从事件日志生成 replay inspection bundle 和 dataset 草稿、生成复核队列、审核 dataset、统计多人复核状态/一致性、校验 judge registry/version、写入 judge 人工 label、对比报告、记录本地趋势,并提供 rule judge / Ollama LLM judge 骨架;但还没有接入真实 OIDC/JWT provider、集中式租户策略服务、多人协作的线上回放与标注平台。
