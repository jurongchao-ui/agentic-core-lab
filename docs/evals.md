# Agentic Core Evals

## 目标

Eval harness 用来把 agentic 行为变成可回归的质量门禁。

它不是只看最终答案,还检查:

- 工具是否被正确调用。
- 工具失败是否被正确处理。
- 记忆是否真实保存。
- ResponsePolicy 是否走到正确档位。
- SafetyPolicy 是否拦截。
- 事件是否按预期 emit。
- 汇总指标是否超过阈值。

## 运行

文本报告:

```bash
python3 -m agentic_core.eval_harness
```

JSON 报告:

```bash
python3 -m agentic_core.eval_harness --json
```

用例失败或 gate 失败时退出码非 0,适合接 CI。

启用回答质量 judge:

```bash
python3 -m agentic_core.eval_harness --judge rule
AGENTIC_MODEL=openhermes:latest python3 -m agentic_core.eval_harness --judge llm
```

`--judge rule` 使用完全离线的确定性裁判,检查状态、工具、回答片段和
ResponsePolicy tier 是否满足期望。`--judge llm` 使用本地 Ollama 模型做
LLM-as-judge,模型不可用或输出非法时会回退到 rule judge。默认 `--judge off`,
保持原来的纯确定性 eval 行为。

管理 judge rubric 注册表:

```bash
python3 -m agentic_core.eval_judge_registry list
python3 -m agentic_core.eval_judge_registry validate --input data/eval-golden.json
python3 -m agentic_core.eval_judge_registry validate --input data/eval-golden.json --json
```

dataset 中的 `judgeRubric` / `judgeRubricVersion` 必须能在本地注册表中找到。
启用 judge 时,eval 也会检查当前 judge 的 rubric 与 case 期望是否匹配。
因此只有当 dataset case 也标注为 `strict_answer_quality:v1` 时,才应该运行:

```bash
python3 -m agentic_core.eval_harness --cases data/eval-golden.json --judge rule --judge-rubric strict_answer_quality --judge-rubric-version v1
```

从事件日志生成待审核 dataset 草稿:

```bash
python3 -m agentic_core.eval_dataset --backend jsonl --path data/events.jsonl --output data/eval-dataset.json
python3 -m agentic_core.eval_dataset --backend sqlite --path data/events.db --output data/eval-dataset.json
```

从事件日志生成 replay inspection bundle:

```bash
python3 -m agentic_core.eval_replay --backend jsonl --path data/events.jsonl --run-id run_xxx
python3 -m agentic_core.eval_replay --backend sqlite --path data/events.db --run-id run_xxx --json
```

`eval_replay` 不会重新执行 LLM 或工具,它只把某个 `runId` 的历史事件整理成
可复盘的结构化 bundle,用于 debug、人工复核和后续线上回放平台。

从 dataset 生成复核队列:

```bash
python3 -m agentic_core.eval_sampling --input data/eval-dataset.json
python3 -m agentic_core.eval_sampling --input data/eval-dataset.json --reason needs_judge_label --limit 20 --json
python3 -m agentic_core.eval_sampling --input data/eval-dataset.json --output data/eval-review-queue.json
```

`eval_sampling` 会按优先级挑出更值得人工先看的 case。默认优先级原因包括:

- `review_required`
- `needs_answer_label`
- `needs_judge_label`
- `safety_case`
- `tool_failure_case`
- `memory_write_case`

用 dataset 跑回归:

```bash
python3 -m agentic_core.eval_harness --cases data/eval-dataset.json
python3 -m agentic_core.eval_harness --cases data/eval-dataset.json --json
```

审核 dataset 草稿并要求只跑已审核 case:

```bash
python3 -m agentic_core.eval_review list --input data/eval-dataset.json
python3 -m agentic_core.eval_review apply --input data/eval-dataset.json --output data/eval-golden.json --approve-all --reviewer local
python3 -m agentic_core.eval_harness --cases data/eval-golden.json --require-reviewed
```

统计多人复核一致性:

```bash
python3 -m agentic_core.eval_review state --input data/eval-golden.json
python3 -m agentic_core.eval_review state --input data/eval-golden.json --score-tolerance 5 --json
python3 -m agentic_core.eval_review agreement --input data/eval-golden.json
python3 -m agentic_core.eval_review agreement --input data/eval-golden.json --score-tolerance 5 --json
```

`state` 输出治理后台所需的当前状态视图: 每个 case 当前状态、reviewer、
review session、reviewCount、latestDecision 和 conflicts。

`agreement` 会基于 `reviewDecisions` 统计同一个 case 的多位 reviewer 是否一致,
并识别 `status_conflict`、`judge_passed_conflict`、`judge_score_drift`。

使用 SQLite 保存多人审核决策:

```bash
python3 -m agentic_core.eval_review_store init --path data/reviews.db
python3 -m agentic_core.eval_review_store import --path data/reviews.db --input data/eval-golden.json
python3 -m agentic_core.eval_review_store list --path data/reviews.db
python3 -m agentic_core.eval_review_store query --path data/reviews.db --reviewer local --limit 20 --offset 0 --json
python3 -m agentic_core.eval_review_store state --path data/reviews.db --input data/eval-golden.json --json
```

`eval_review_store` 把 `reviewDecisions` 存进 SQLite。启用它后,dataset JSON 仍负责
提供 case 元数据,SQLite store 负责保存和查询多人审核 decision,避免服务端协作状态
只能依赖某一个 JSON 文件。`data/*.db` 已加入 `.gitignore`,本地运行产物不会进入仓库。

审核时写入人工 judge label:

```bash
python3 -m agentic_core.eval_review apply \
  --input data/eval-dataset.json \
  --output data/eval-golden.json \
  --approve-all \
  --reviewer local \
  --judge-rubric strict_answer_quality \
  --judge-rubric-version v1 \
  --expected-judge-score 95 \
  --expected-judge-passed true \
  --judge-score-tolerance 5 \
  --judge-notes "人工确认回答完整清晰"
python3 -m agentic_core.eval_harness --cases data/eval-golden.json --require-reviewed --judge rule
```

`eval_review` 会给通过审核的 case 写入 `reviewRequired=false`、`reviewStatus=approved`、
`reviewedAt`、`reviewer` 和 `reviewNotes`。被 reject 的 case 默认不进入输出 `cases`,
但会保存在 `reviewDecisions` 里作为审计记录。

如果 case 带有 `expectedJudgeScore` / `expectedJudgePassed`,启用 `--judge` 后,
eval 会把 judge 输出与人工 label 比对。分数偏离超过 `judgeScoreTolerance` 或通过状态
不一致时,case 会失败。这是本地版 judge calibration 闭环。

对比两次 eval JSON 报告:

```bash
python3 -m agentic_core.eval_harness --json > data/eval-base.json
python3 -m agentic_core.eval_harness --json > data/eval-candidate.json
python3 -m agentic_core.eval_diff --base data/eval-base.json --candidate data/eval-candidate.json
python3 -m agentic_core.eval_diff --base data/eval-base.json --candidate data/eval-candidate.json --json
python3 -m agentic_core.eval_diff --base data/eval-base.json --candidate data/eval-candidate.json --fail-on-regression
```

`eval_diff` 会比较 gate、metrics、case pass/fail 和 event count。加上 `--fail-on-regression`
后,只要发现 gate 从 PASS 变 FAIL、case 从 pass 变 fail、或关键指标退化,命令就返回非 0。

追加到本地趋势历史:

```bash
python3 -m agentic_core.eval_harness --json > data/eval-current.json
python3 -m agentic_core.eval_history append --report data/eval-current.json --history data/eval-history.jsonl --label local
python3 -m agentic_core.eval_history list --history data/eval-history.jsonl
python3 -m agentic_core.eval_history list --history data/eval-history.jsonl --json
```

`eval_history` 使用 JSONL append-only 存储。每条记录保存完整 report 和一份 summary,
可以看到最近几次 gate、pass rate、tool success rate,并检测最新一次相对上一次的指标退化。

生成本地治理 dashboard:

```bash
python3 -m agentic_core.eval_dashboard \
  --report data/eval-current.json \
  --history data/eval-history.jsonl \
  --dataset data/eval-golden.json \
  --output data/eval-dashboard.html \
  --json-output data/eval-dashboard.json
```

`eval_dashboard` 会把 report、history、review queue、agreement、rubric validation 聚合成
一个单文件 HTML 和可选 JSON summary。它是服务端可视化治理后台的本地静态版。

启动只读本地治理服务:

```bash
python3 -m agentic_core.eval_server \
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
python3 -m agentic_core.eval_server \
  --report data/eval-current.json \
  --history data/eval-history.jsonl \
  --dataset data/eval-dataset.json \
  --review-output data/eval-golden.json \
  --review-store data/reviews.db \
  --audit-events data/eval-server-audit.jsonl
```

开启后,除 `/health` 外都需要请求头:

```text
Authorization: Bearer local-secret
```

也可以拆分只读和审核 token:

```bash
AGENTIC_EVAL_SERVER_VIEWER_TOKEN=view-secret \
AGENTIC_EVAL_SERVER_REVIEWER_TOKEN=review-secret \
python3 -m agentic_core.eval_server \
  --dataset data/eval-dataset.json \
  --review-output data/eval-golden.json \
  --review-store data/reviews.db \
  --audit-events data/eval-server-audit.jsonl
```

或者使用 signed claims token。它用 HMAC 签名 claims,包含 `sub`、`tenant`、`scopes`、
`iat`、`exp`,比纯静态 token 更接近真实 JWT/session 的生产边界:

```bash
TOKEN=$(python3 -m agentic_core.auth_tokens create \
  --secret local-signing-secret \
  --subject reviewer_1 \
  --tenant default_tenant \
  --scopes eval.viewer,eval.reviewer \
  --ttl 3600)

AGENTIC_EVAL_SERVER_SIGNING_SECRET=local-signing-secret \
AGENTIC_EVAL_SERVER_TENANT_POLICY=data/tenant-policy.json \
python3 -m agentic_core.eval_server \
  --dataset data/eval-dataset.json \
  --review-output data/eval-golden.json \
  --review-store data/reviews.db
```

请求时:

```text
Authorization: Bearer $TOKEN
```

tenant policy JSON 示例:

```json
{
  "schemaVersion": 1,
  "tenants": {
    "default_tenant": {
      "enabled": true,
      "allowedScopes": ["eval.viewer", "eval.reviewer"]
    },
    "suspended_tenant": {
      "enabled": false,
      "allowedScopes": ["eval.viewer"]
    }
  }
}
```

查看策略:

```bash
python3 -m agentic_core.tenant_policy show --path data/tenant-policy.json
```

本地 scope 规则:

- `--auth-token` / `AGENTIC_EVAL_SERVER_TOKEN`: admin token,拥有 `eval.viewer` 和 `eval.reviewer`。
- `--viewer-token` / `AGENTIC_EVAL_SERVER_VIEWER_TOKEN`: 只读 token,只拥有 `eval.viewer`。
- `--reviewer-token` / `AGENTIC_EVAL_SERVER_REVIEWER_TOKEN`: 审核 token,拥有 `eval.viewer` 和 `eval.reviewer`。
- `--signed-token-secret` / `AGENTIC_EVAL_SERVER_SIGNING_SECRET`: 验证 signed claims token 的 HMAC 密钥,scope 从 token claims 读取。
- `--tenant-policy` / `AGENTIC_EVAL_SERVER_TENANT_POLICY`: 可选租户策略 JSON。启用后,token 所属 tenant 必须存在、启用,并允许当前请求需要的 scope。静态 token 会使用 `local_static` tenant。
- `GET /dashboard`、`GET /api/dashboard`、`GET /api/rubrics`、`GET /api/reviews/status` 需要 `eval.viewer`。
- `POST /api/reviews/apply` 需要 `eval.reviewer`。

常用只读路由:

- `GET /health`: 服务健康检查。
- `GET /dashboard`: HTML dashboard。
- `GET /api/dashboard`: dashboard JSON summary。
- `GET /api/rubrics`: 当前 judge rubric 注册表。
- `GET /api/reviews/status`: 多用户审核状态视图。
- `GET /api/reviews/decisions`: SQLite review store 的审核流水分页查询。
- `POST /api/reviews/apply`: 应用 approve/reject 决策,写出服务端配置的 golden dataset。

`POST /api/reviews/apply` 的安全边界:

- 必须配置 `--auth-token` 或 `AGENTIC_EVAL_SERVER_TOKEN`。
- 必须配置服务端 `--dataset` 和 `--review-output`。
- 请求体只能传审核决策,不能传输入/输出路径。
- 可选配置 `--review-store` 或 `AGENTIC_EVAL_SERVER_REVIEW_STORE`,把新增
  `reviewDecisions` 同步写入 SQLite,`GET /api/reviews/status` 会优先从该 store
  汇总多人审核状态,`GET /api/reviews/decisions` 可按 `caseName`、`reviewer`、
  `reviewSessionId`、`limit`、`offset` 查询审核流水。
- 可选配置 `--audit-events` 或 `AGENTIC_EVAL_SERVER_AUDIT_EVENTS`,将审核写入记录到 JSONL。

示例请求体:

```json
{
  "approve": ["case_needs_review"],
  "reject": [],
  "reviewer": "local",
  "reviewSessionId": "session_20260704_a",
  "notes": "人工确认可进入 golden dataset",
  "judgeRubric": "strict_answer_quality",
  "judgeRubricVersion": "v1",
  "expectedJudgeScore": 95,
  "expectedJudgePassed": true,
  "judgeScoreTolerance": 5,
  "judgeNotes": "回答完整清晰"
}
```

`eval_server` 复用 `eval_dashboard`、`eval_review` 和 `eval_review_store` 的核心逻辑,提供标准库
`http.server` 治理入口。它已有本地 viewer/reviewer scope、最小受保护写入 API 和
`eval_review_apply` / `eval_review_apply_failed` 审计事件;配置 `--review-store` 后,
审核 decision 会进入 SQLite,状态查询也会从 SQLite 派生。它仍还不是完整协作标注平台:
当前没有真实用户/JWT、租户策略中心、服务端分页列表或专门的标注 UI。

`eval_dataset` 生成的是草稿,每条 case 默认 `reviewRequired=true`。它会从历史 run 中抽取:

- `goal`
- `expectedStatus`
- `expectedTools`
- `expectedResponseTiers`
- `expectedMemorySaves`
- `expectedSafetyRefusal`
- `expectedToolFailures`
- `expectedEventCounts`
- `observedAnswer`
- `judgeRubric`
- `judgeRubricVersion`
- `expectedJudgeScore`
- `expectedJudgePassed`
- `judgeScoreTolerance`
- `judgeNotes`

进入长期 golden dataset 前,应该人工确认哪些自然语言片段适合保留为 `expectedAnswerContains`。

`eval_sampling` 输出 `agentic_eval_review_queue`,用于把待审核 dataset 转成可排序的复核任务:

```text
schemaVersion
type
generatedAt
source
samplePolicy
summary
items
```

`eval_replay` 输出 `agentic_eval_replay_bundle`:

```text
schemaVersion
type
generatedAt
source
runId
goal
status
answer
eventCounts
toolCalls
safety
memoryDecision
responseDecision
timeline
events
```

## 默认用例

当前默认 8 个确定性用例:

```text
calculator_note
memory_preference_save
study_plan_uses_memory
safety_refusal
sensitive_memory_rejected
tech_stack_clarification
tech_stack_save
failed_calculation_blocks_note
```

覆盖点:

- 计算 + 记笔记。
- 长期偏好保存。
- 长期记忆影响学习计划。
- global safety 拒绝。
- 敏感记忆拒绝。
- 技术栈缺信息追问。
- 技术栈保存。
- 计算失败不写笔记。

## 报告结构

`EvalReport` 包含:

```text
total
passed
failed
passedGate
metrics
eventCounts
thresholds
gateFailures
cases
```

启用 judge 后,每个 `EvalCaseResult` 会额外带上 `judge`:

```text
passed
score
reason
rubric
metadata
```

judge 失败会被追加到 case failures,因此也会影响 `passedGate`。

`eval_diff` 输出:

```text
basePath
candidatePath
basePassedGate
candidatePassedGate
gateRegression
hasRegression
metricDiffs
eventCountDiffs
caseDiffs
```

`eval_history` summary 输出:

```text
totalRecords
gatePassRate
latest
previous
metricDeltas
regressions
recent
```

`eval_sampling` queue item 输出:

```text
queueId
caseName
goal
priority
reasons
sourceRunId
reviewRequired
case
```

`eval_review agreement` 输出:

```text
schemaVersion
type
generatedAt
scoreTolerance
summary
cases
```

`eval_review state` 输出:

```text
schemaVersion
type
generatedAt
scoreTolerance
summary
cases
```

每个 state case 包含:

```text
caseName
goal
currentStatus
reviewRequired
needsReview
reviewers
reviewSessions
reviewCount
statuses
conflicts
latestDecision
```

`GET /api/reviews/decisions` 和 `eval_review_store query` 输出:

```text
schemaVersion
type
generatedAt
reviewStore
filters
pagination
decisions
```

`eval_review_store` SQLite 表核心字段:

```text
id
case_name
status
reviewer
review_session_id
notes
reviewed_at
judge_labels_json
decision_json
```

`eval_judge_registry validate` 输出:

```text
schemaVersion
type
valid
validCount
invalidCount
invalid
knownRubrics
```

`eval_dashboard` JSON summary 输出:

```text
schemaVersion
type
generatedAt
inputs
reportSummary
historySummary
reviewQueueSummary
agreementSummary
rubricValidation
```

审核后的 dataset case 会额外包含:

```text
reviewRequired
reviewStatus
reviewedAt
reviewer
reviewSessionId
reviewNotes
judgeRubric
judgeRubricVersion
expectedJudgeScore
expectedJudgePassed
judgeScoreTolerance
judgeNotes
```

每个 `EvalCaseResult` 包含:

```text
name
passed
failures
answer
status
tool_names
response_tiers
memory_texts
event_counts
metrics
judge
```

## 汇总指标

```text
case_pass_rate
tool_calls
tool_failures
tool_success_rate
planner_fallbacks
safety_refusals
memory_saved
memory_decisions
run_failed
avg_steps
judge_evaluated
judge_passed
judge_pass_rate
```

## 默认阈值

`EvalThresholds`:

```text
minCasePassRate = 1.0
minToolSuccessRate = 0.75
maxRunFailed = 0
maxPlannerFallbacks = 0
```

`failed_calculation_blocks_note` 会产生一次预期工具失败,所以默认工具成功率阈值不是 1.0。

## 扩展方式

新增用例时优先断言稳定结构:

- `expected_status`
- `expected_tools`
- `expected_response_tiers`
- `expected_event_counts`
- `expected_memory_saves`
- `expected_tool_failures`
- `expected_memory_contains`

少绑定完整自然语言回复,多绑定关键片段和结构化事件。

## Dataset 文件

dataset 支持两种形状:

```json
{
  "schemaVersion": 1,
  "type": "agentic_eval_dataset",
  "generatedAt": "2026-07-03T00:00:00+00:00",
  "source": {
    "kind": "event_log",
    "backend": "jsonl",
    "path": "data/events.jsonl"
  },
  "cases": []
}
```

也可以直接传 case 列表:

```json
[
  {
    "name": "calculator_note",
    "goal": "帮我计算 128 * 7, 然后记录成学习笔记",
    "expectedTools": ["calculator", "note.add"]
  }
]
```
