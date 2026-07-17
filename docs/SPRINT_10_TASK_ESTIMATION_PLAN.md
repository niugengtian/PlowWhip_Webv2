# Sprint 10 Task Estimation Plan

## Scope

本文件是协调/PM Worker 对 Sprint 10 任务估算与角色切片的可执行契约。只新增本文档，不修改生产代码、测试、配置、迁移或现有文档，不提交不推送。

现场事实基线：

- 刚完成的中型 Backend 切片耗时约 125 秒，Host 流约 523 KiB，19 项测试通过，Cursor 新 Session 报告 118495 Token。
- 任务创建时页面静默套用 50000 Token 和 600 秒；运行中页面显示 0/50000；最终 118495/50000 仍被判 completed。
- 当前 `web/src/App.tsx` 不让用户填写 `token_budget`、`max_attempts`、`timeout`；Host 任务固定 `timeout=600`；`api/schemas.py` 上限 600；`settings_repository.py` 只有全局默认 50000；`TaskService` / Host Bridge 没有进展驱动的软截止。
- 旧 plow-whip 已有：1-7 个里程碑、每个里程碑内部完成理解/实现/测试/文档、拒绝过细拆分、Cursor/Codex 分别 300/1800 默认、Session resume、零 Token 探针和有界熔断。

严格区分：旧版能力是拆分原则、provider 默认、Session resume、零 Token 探针和有界熔断；本次新增的是统一 `TaskSizing` / `ExecutionBudget` 契约、估算前置门、p90 预算预留、活动 deadline、预算执行、UI 展示与人工覆盖。

## Unified Contract

只允许一套 `TaskSizing` / `ExecutionBudget` 契约作为任务估算和运行预算来源，禁止再新增彼此打架的 timeout 状态。现有 50000 Token / 600 秒只能作为 bootstrap fallback 的历史输入，不能继续作为静默默认。

数据流：

```text
Dispatch Gate -> TaskSizing Inputs -> TaskSizing Output -> ExecutionBudget
-> pre-dispatch p90 reservation -> runtime enforcement -> completion evidence
```

`TaskSizing` 输出：

- `size_class`: `XS | S | M | L | XL`
- `rationale`: 机器可审计依据，必须引用结构化输入，不得写模型自信或标题感觉。
- `estimated_input_tokens`: `{ min, max, p90 }`
- `estimated_output_tokens`: `{ min, max, p90 }`

`ExecutionBudget` 输出：

- `soft_deadline_seconds`
- `hard_deadline_seconds`
- `max_turns`
- `max_attempts`
- `verification_timeout_seconds`
- `progress_extension_seconds`
- `total_token_hard_cap`
- `reserved_tokens`

## Dispatch Gate

任务派发前必须满足四个前置门；任一缺失则不得派发，状态进入 `needs_planning`：

- 可验证产物：能说明完成后可观察、可检查的产物，例如文件、API 行为、测试结果、部署状态或迁移结果。
- 文件/组件边界：能列出允许修改的文件、目录或组件边界。
- 验证命令：能给出确定性验证命令和预期结果。
- 外部依赖：能声明网络、Provider、其他任务产物、人工验收等依赖是否存在且是否已就绪。

计划保持旧版已验证的 1-7 个有业务结果的里程碑。每个里程碑内部包含理解、实现、测试、文档闭环；拒绝把每个动作碎拆成独立任务。

## Machine-Readable Sizing Inputs

估算只能使用可机器解释的量级输入：

- `layers_touched`: 涉及层数，例如 backend API、domain、runtime、store、frontend、deploy。
- `components_touched`: 涉及组件数。
- `files_expected`: 预计修改文件数量。
- `has_migration`: 是否涉及迁移。
- `has_deploy_step`: 是否涉及部署或切换。
- `verification_commands_count`: 验证命令数量。
- `verification_estimated_seconds`: 验证预计时长。
- `external_dependencies`: 外部依赖列表。
- `risk_level`: `low | medium | high`。
- `needs_independent_acceptance`: 是否需要独立验收。

明确禁止仅凭标题长度、描述字数、模型自信评分或“看起来简单/复杂”来定级。

## Bootstrap Size Classes

Bootstrap 档位只用于冷启动。后续必须用历史已完成任务的实际 token、耗时、attempt、验证结果校准各档 p90；不能把 200000 或任何临时默认固化成万能值。

| size_class | token range | token p90 | soft deadline | hard deadline | max_turns | max_attempts | verification_timeout | progress_extension |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| XS | 5k-30k | 25k | 120s | 300s | 10 | 2 | 60s | 60s |
| S | 20k-80k | 60k | 240s | 600s | 20 | 2 | 120s | 90s |
| M | 60k-200k | 150k | 480s | 1200s | 40 | 3 | 300s | 120s |
| L | 150k-500k | 400k | 900s | 2400s | 80 | 3 | 600s | 180s |
| XL | 400k-1M | 800k | 1800s | 4800s | 120 | 4 | 900s | 300s |

最小值与最大值：

- `total_token_hard_cap = (estimated_input_tokens.p90 + estimated_output_tokens.p90) * 1.5`
- 全局最小 hard cap：25k Token。
- 全局最大 hard cap：1.5M Token。
- 同一 `size_class` 累计至少 10 个已完成样本后，用实际分布 p90 替换 bootstrap p90。
- 人工覆盖的任务必须记录 `manual_override=true`，校准时单独分桶，不能污染自动估算基线。

## Active Deadline

软截止只能由确定性进展信号延长。允许延长软截止的信号：

- 新 artifact 产生或已有 artifact hash 变化。
- 验证项从失败转为通过。
- 有效 output segment 或检查点产生。
- 明确 step 或里程碑完成。

不算进展的信号：

- 单纯 heartbeat。
- 重复日志。
- token 增长。
- provider 仍在输出但没有结构化产物或检查点。

硬边界绝不自动增长：

- `hard_deadline_seconds` 不自动延长。
- `total_token_hard_cap` 不自动增加。
- 最大 Run / `max_turns` 不自动增加。

接近边界时的流程：

- 达到软/硬/token/run 边界预警阈值时，先持久化 carry-forward，包括已完成产物、未完成项、验证状态、当前预算用量和下一步建议。
- 优先使用旧版已有 Session resume 续接同 Session，避免重新建立上下文。
- 同 Session 续接后仍超界，则重新拆分进入 `needs_planning`，或进入 `needs_human`。
- 不允许靠 heartbeat、重复日志、模型自报“快完成了”或 token 增长无限延长。

## Budget Enforcement

派发前：

- Scheduler 必须按 p90 原子预留 `reserved_tokens`。
- 预留失败时任务 defer，且 defer 原因必须可见。
- 不允许静默降级到 50000/600。

运行中：

- 从 usage stream 实时更新 token 用量。
- 达到 `reserved_tokens` 80% 时发出预警。
- 达到 100% 时软停：不再启动新的模型 Run，允许当前 Run 收敛到最近检查点。
- 网络/Provider 中断不消耗实现 attempt；必须通过零 Token 探针或等价 evidence 区分链路失败与实现失败。

完成判定：

- 超预算不得判 `completed`。
- 唯一例外：验证产物已经完成，且系统记录显式 `budget_overrun` evidence，包括估算值、实际值、完成验证、超出原因和禁止继续新模型 Run 的状态。
- 没有 `budget_overrun` evidence 的 118495/50000 不能再被判 `completed`。

## UI Requirements

创建前 UI 必须展示：

- `size_class`
- `rationale`
- input/output Token 区间与 p90
- soft/hard deadline
- `max_turns`
- `max_attempts`
- `verification_timeout_seconds`
- `progress_extension_seconds`
- `total_token_hard_cap`

创建前 UI 必须允许人工覆盖：

- token p90 或 hard cap
- soft/hard deadline
- `max_turns`
- `max_attempts`
- verification timeout

人工覆盖必须被记录为 `manual_override`，并在任务详情与后续校准数据中可见。

任务详情 UI 必须实时显示：

- output 流。
- heartbeat 状态。
- 最近一次确定性进展信号及其类型。
- token 用量、预留量、hard cap。
- soft/hard deadline 倒计时。
- 当前 attempts / max_attempts。
- Scheduler defer 原因。

## Role Slices

四个切片都按约 8 分钟设计。优先复用当前 dirty worktree。禁止不同角色同时编辑同一文件。

### 1. Backend Policy

允许修改文件：

- `backend/plow_whip_web/api/schemas.py`
- `backend/plow_whip_web/domain/model.py`
- `backend/plow_whip_web/runtime/budget.py`
- `backend/plow_whip_web/store/settings_repository.py`
- `backend/plow_whip_web/store/task_repository.py`
- `backend/plow_whip_web/store/migrations/0016_task_sizing_budget.sql`
- `tests/test_budget_policy.py`

依赖与冲突：

- 第一个执行。
- Host/Scheduler 依赖本切片产出的模型和预算策略。
- 不得修改 `host_bridge.py`、`task_service.py`、`web/src/App.tsx`。

可直接粘贴到页面的目标文本：

```text
实现统一 TaskSizing/ExecutionBudget 后端策略契约。新增可机器解释的 sizing inputs、size_class、rationale、estimated input/output token 区间和 p90、soft/hard deadline、max_turns、max_attempts、verification_timeout、progress_extension、total_token_hard_cap、reserved_tokens。用 XS/S/M/L/XL bootstrap 档位冷启动，并把 50000/600 降级为无估算时的历史 fallback，不再静默套用。api schema 移除固定 timeout≤600 的任务预算含义，改为接受 ExecutionBudget。新增迁移保存估算、实际用量、manual_override 和 budget_overrun evidence 字段。禁止修改 Host/Scheduler 和 Frontend 文件。
```

质量门：

- `pytest tests/test_budget_policy.py`
- 覆盖档位映射、p90 hard cap、最小/最大值、manual override 标记、禁止标题长度/模型自信定级。

### 2. Host/Scheduler

允许修改文件：

- `backend/plow_whip_web/host_bridge.py`
- `backend/plow_whip_web/runtime/task_service.py`
- `backend/plow_whip_web/runtime/context.py`
- `backend/plow_whip_web/store/host_job_repository.py`
- `tests/test_active_deadline.py`
- `tests/test_host_job_continuity.py`

依赖与冲突：

- 依赖 Backend Policy 的 `ExecutionBudget` 字段。
- 可与 Frontend/UI 在不同文件上并行。
- 不得修改 `schemas.py`、`domain/model.py`、`runtime/budget.py`、`settings_repository.py`、`web/src/App.tsx`。

可直接粘贴到页面的目标文本：

```text
实现活动 deadline 和预算执行。软截止只允许由新 artifact/hash、验证失败转通过、有效 output segment/检查点、明确 step 完成四类确定性进展延长；heartbeat、重复日志、token 增长不算进展。hard deadline、total token hard cap、最大 Run/max_turns 不自动增长。接近边界先持久化 carry-forward，并优先 resume 同 Session；仍超界则 needs_planning 或 needs_human。从 usage stream 实时更新 token，80% 预警，100% 软停且禁止新模型 Run。超预算不得 completed，除非验证产物完成且记录 explicit budget_overrun evidence。网络/Provider 中断不消耗实现 attempt。
```

质量门：

- `pytest tests/test_active_deadline.py tests/test_host_job_continuity.py`
- 覆盖进展信号延长、heartbeat 不延长、hard cap 不增长、usage stream 预警/软停、超预算 completed 禁止、provider 中断不计 attempt。

### 3. Frontend/UI

允许修改文件：

- `web/src/App.tsx`
- `web/src/**` 中与任务创建表单、任务详情、API client、组件测试直接相关的前端文件。

依赖与冲突：

- 依赖 Backend Policy 的 API 字段；可先按契约字段实现。
- 可与 Host/Scheduler 并行。
- 不得修改 `backend/**`。

可直接粘贴到页面的目标文本：

```text
实现任务创建前的预算估算展示与人工覆盖 UI。创建表单展示 size_class、rationale、Token 区间与 p90、soft/hard deadline、max_turns、max_attempts、verification_timeout、progress_extension、total_token_hard_cap，并允许人工覆盖且提交 manual_override。任务详情实时显示 output、heartbeat、最近确定性进展信号、token 用量/预留/hard cap、soft/hard deadline 倒计时、attempts 和 Scheduler defer 原因。修复运行中显示 0/50000 的体验，改为消费 usage stream。
```

质量门：

- `npm run build`
- 前端组件测试覆盖创建前估算展示、人工覆盖、详情页 usage 更新、defer 原因可见。

### 4. QA/Deploy

允许修改文件：

- `tests/test_budget_e2e.py`
- 部署切换记录文件仅在后续实施任务明确允许时新增；本协调任务不新增。

依赖与冲突：

- 必须在 Backend Policy、Host/Scheduler、Frontend/UI 完成后执行。
- 不修改生产代码。
- 发现缺陷回报对应切片，不在 QA/Deploy 切片中抢修。

可直接粘贴到页面的目标文本：

```text
做预算估算与执行的端到端验收。创建 M 级任务，断言创建响应包含 size_class、rationale、p90 和 ExecutionBudget，而不是静默 50000/600。模拟 usage stream，断言运行中 token 不再显示 0/50000。模拟 heartbeat、重复日志和 token 增长，断言不会延长 soft deadline。模拟有效 artifact/hash、验证失败转通过、output checkpoint、step 完成，断言可有界延长 soft deadline。模拟超预算无 budget_overrun evidence，断言不得 completed；有完成验证和 explicit budget_overrun evidence 时允许 completed 但禁止新模型 Run。记录部署切换点：迁移 -> 后端重启 -> 前端发布；记录回滚点。
```

质量门：

- `pytest tests/`
- `npm run build`
- 手工验收创建页、详情页、Scheduler defer 原因、budget_overrun evidence 展示。

## Deployment Switch Points

- Backend Policy：迁移落库后启用新字段，但默认不改变运行中任务。
- Host/Scheduler：后端重启后启用活动 deadline 与预算执行；旧任务如果没有 `ExecutionBudget`，只允许走 fallback 并标记 deprecated。
- Frontend/UI：前端发布后创建页和详情页展示新预算字段；后端未就绪时应显示不可派发或 needs_planning，而不是静默套默认。
- QA/Deploy：整体切换顺序为迁移、后端重启、前端发布、端到端验收。

## Now Not Doing

现在不做：

- 复杂 DAG 调度。
- 按模型自报动态上涨预算。
- 为每个异常新增分支。
- 无限延长 soft deadline。
- 自动增长 hard Token、Run 或 Deadline。
- 接入更多 Provider。
- 重做全仓审查。
- 修改生产代码、测试、配置、迁移或现有文档。

## Verification For This Coordination Task

本协调任务的完成证据只限于：

- `docs/SPRINT_10_TASK_ESTIMATION_PLAN.md` 存在。
- 文档包含拆分前置门、1-7 里程碑原则、机器可解释量级输入、估算输出、活动 deadline、预算执行、UI 要求、四个 8 分钟角色切片、质量门、部署切换点和 now-not-doing。
- 完成标记只出现一次，且是文件最后独立一行。

SPRINT10_TASK_ESTIMATION_PLAN_COMPLETE
