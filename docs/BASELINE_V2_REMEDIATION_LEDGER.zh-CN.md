# PlowWhip Web V2 基线修复台账

## 1. 权威输入与边界

- 唯一基线：`/Users/niugengtian/work/plow-whip-web-v2/docs/MINIMAL_REDESIGN_BASELINE_V2.zh-CN.md`
- 基线 SHA-256：`4ae79ba906997640304a202ba0d453e2437e77e36468c29eb4a185ad970f6098`
- 差距报告：`docs/BASELINE_V2_CONFORMANCE_AUDIT.zh-CN.md`
- 差距报告 SHA-256：`9b553d741728dc1c5e843148ac28e76efc4a461ac6d74a05d060e77038c72cd8`
- 修复起点：`main@942f246d7b82dcfdcb74452c654646185a27067f`
- 起点 tree：`4b403873193070c374703129a75b28bbd0e53c26`
- 起点工作区：只有未跟踪的差距报告；必须原样保留。
- 起点回归：Python 3.13 下 `67 tests OK, skipped=1`。
- 现场边界：Docker 服务使用端口 `8750`。源码验证可按需重建和重启该容器，但不得调用模型或外部 Provider。
- 兼容边界：SQLite 只允许 additive-compatible 变化；不新增重复状态机、重复队列或第二套运行时真源。

状态枚举：`待复核 / 进行中 / 已实现待回归 / 已闭环 / 受外部环境限制`。

## 2. 根因修复顺序

| ID | 优先级 | 合并根因 | 条款 | 当前生产路径 | 预计修改文件 | 最小验证 | 状态 |
|---|---|---|---|---|---|---|---|
| RC-P0-01 | P0 | 所有正式指令统一进入模型 Planner，启发式仅为输入事实；角色阻塞只经 Butler 串行呈现 | P-01/P-02/P-05/P-06/P-14/P-15/C-01 | `POST /api/messages → intake/butler → lifecycle._create_task → planner` | `plowwhip/planner.py`、`plowwhip/lifecycle.py`、`plowwhip/execution.py`、`plowwhip/verification.py`、`plowwhip/provider.py`、`plowwhip/host_bridge.py`、`tests/test_vertical_slice.py`、`tests/test_host_bridge.py` | simple/medium/large 三类都先形成 Planner HostJob 与结构化 PlannerResult；静态断言 regex/kind 不决定最终 size；跨项目仅一个 `waiting=true` Butler question | 已闭环 |
| RC-P0-02 | P0 | large 方案客观选择、语义原子 TaskSpec 与有依据升级 | P-09/P-10/P-11/P-12/O-05/A-03/T-02 | `planner_prompt/parse/normalize_plan → lifecycle._materialize_plan/_install_plan` | `plowwhip/planner.py`、`plowwhip/lifecycle.py`、`tests/test_vertical_slice.py` | 拒绝无结果合同、无 runtime、非原子、无覆盖映射和自报 0.95 的 Plan | 已闭环 |
| RC-P0-03 | P0 | lifecycle 成为 Goal/Task 生命周期字段唯一写入者 | L-01/L-03/D-09/B-04/B-05/C-02/C-04 | `advance_project` 调用 execution/verification/provider/cronner | `plowwhip/lifecycle.py`、`plowwhip/execution.py`、`plowwhip/verification.py`、`plowwhip/provider.py`、`plowwhip/cronner.py`、`plowwhip/store.py`、测试 | SQL authorizer/AST 边界测试禁止 lifecycle 外更新 Goal/Task 生命周期列 | 已闭环 |
| RC-P0-04 | P0 | 完整结果、Artifact manifest 与完整下游输入 | A-23/R-01～R-08/R-12/R-14～R-16/D-06/D-11/D-13 | `execution finalize → artifacts → continuity dependencies → verification manifest` | `plowwhip/execution.py`、`plowwhip/continuity.py`、`plowwhip/verification.py`、`plowwhip/store.py`、测试 | 超过 1 MiB 的交付物完整读取；任一 path/hash/revision/scope/source 篡改均 fail closed；tail 不进入输入 | 已闭环 |
| RC-P0-05 | P0 | 所有模型产出统一由独立 Checker 验收 | A-16/A-17/A-18/R-08/R-13 | Planner/Worker/probe 模型 HostJob → 正式结果 → Checker | `plowwhip/lifecycle.py`、`plowwhip/execution.py`、`plowwhip/verification.py`、测试 | Planner、Worker、minimal probe 均走独立 TaskSession Checker；Prompt 不含 Worker 自由聊天 | 已闭环 |
| RC-P0-06 | P0 | Task 终态前收敛 HostJob、授权和 Secret 引用 | B-21/S-05 | `cancel/complete action → active HostJob → terminal outcome` | `plowwhip/lifecycle.py`、`plowwhip/execution.py`、`plowwhip/verification.py`、测试 | provider_probe/git_publish 取消时先 stop/reconcile，随后撤销引用，最后写 cancelled/done | 已闭环 |
| RC-P1-01 | P1 | timeout 顺序与冻结 grace | T-02/T-06/T-07/T-09 | Host Bridge deadline → lifecycle reconcile/checkpoint/stop | `plowwhip/host_bridge.py`、`plowwhip/lifecycle.py`、`plowwhip/execution.py`、测试 | deadline、grace、SIGTERM/SIGKILL、晚到结果逐步断言 | 已闭环 |
| RC-P1-02 | P1 | Generation、Warm handoff 与 append-only Cold 无损恢复 | A-14/M-03/M-06/M-09/D-11 | Provider failure/compact → continuity checkpoint → generation replacement | `plowwhip/execution.py`、`plowwhip/continuity.py`、`plowwhip/host_bridge.py`、测试 | 健康 Session 不轮换；替换前校验最新 Warm；Cold segment 不覆盖 | 已闭环 |
| RC-P1-03 | P1 | single/cumulative Token 按物理 Session 归一化 | M-11/M-12/M-13 | Host Bridge usage → adapter fact → model_calls → monitor | `plowwhip/host_bridge.py`、`plowwhip/provider.py`、`plowwhip/execution.py`、`plowwhip/lifecycle.py`、`plowwhip/verification.py`、测试 | 同 generation 累计快照取相邻差值；跨 generation 重置基线 | 已闭环 |
| RC-P1-04 | P1 | Secret 不进入 SQLite/Prompt/模板/日志并在终态失效 | D-30/B-21 | message/action/library/provider env → persistence/prompt/log | `plowwhip/intake.py`、`plowwhip/butler.py`、`plowwhip/lifecycle.py`、`plowwhip/host_bridge.py`、`plowwhip/store.py`、测试 | 注入测试 Secret，扫描 SQLite、Artifact、Prompt、模板和日志均无明文 | 已闭环 |
| RC-P1-05 | P1 | 三档 capability、scope、expiry 运行时硬校验 | D-20/B-17/B-18/B-19 | TaskSpec authorization → execution/Host Bridge | `plowwhip/planner.py`、`plowwhip/execution.py`、`plowwhip/host_bridge.py`、测试 | 未授权不可逆/外部动作 fail closed；删除改为时间戳 `.rm.*` | 已闭环 |
| RC-P1-06 | P1 | 禁止 PASS 自动污染 WorkerTemplate | D-21/D-22 | `verification PASS → project worker_template` | `plowwhip/verification.py`、`plowwhip/lifecycle.py`、测试 | 普通 PASS 不创建模板 revision；显式长期 owner decision 才更新 | 已闭环 |
| RC-P2-01 | P2 | 四类页面、Task-ID 完整文件入口与观察标签 | B-13/B-14/D-15 | UI navigation → GET task/file → monitor references | `plowwhip/app.py`、`plowwhip/monitor.py`、`plowwhip/ui.py`、测试 | 仅四类产品页；ID 解析、边界和 hash 校验；tail 明示非 Evidence | 已闭环 |
| RC-P2-02 | P2 | Provider/model 业务选择移出环境变量 | D-28/D-29 | SQLite settings snapshot → Host Bridge dispatch | `plowwhip/store.py`、`plowwhip/execution.py`、`plowwhip/provider.py`、`plowwhip/host_bridge.py`、测试 | env 不接受业务模型选择；冻结 settings 决定 model | 已闭环 |
| RC-P2-03 | P2 | 当前 Task 验收后的显式脚本入库合同 | D-24/D-25 | done Task Artifact → owner promotion action → library_items | `plowwhip/intake.py`、`plowwhip/lifecycle.py`、`plowwhip/store.py`、测试 | 未完成/未 PASS/非单文件 CLI 拒绝；显式 action 登记 revision/hash | 已闭环 |
| RC-P2-04 | P2 | Butler 精确检索不足时的受控语义归纳 | B-12 | global message → exact SQLite/file search → read-only semantic summary → route | `plowwhip/butler.py`、`plowwhip/lifecycle.py`、`plowwhip/verification.py`、测试 | 精确查询零模型；模糊查询有来源引用、独立验收且不创建业务 Task | 已闭环 |
| RC-P3-01 | P3 | Host Bridge macOS/Linux 契约证据 | B-02 | start/read/stop/resume/segment | `plowwhip/host_bridge.py`、`tests/test_host_bridge.py`、验收 Artifact | 本机 macOS 确定性契约；Linux 通过本地 Docker 隔离契约，不调用 Provider | 已闭环 |
| RC-P3-02 | P3 | Task 详情字段与完整文件呈现 | B-13/Task 详情合同 | monitor snapshot → task detail UI | `plowwhip/monitor.py`、`plowwhip/ui.py`、`plowwhip/app.py`、测试 | role/model/generation/Checker/Evidence/handoff/session segment 可见可验 hash | 已闭环 |
| RC-P3-03 | P3 | Docker 8750 本地运行链验收 | B-03/A-07/E18 | image → single Cronner lease → 8750 → Host Bridge boundary | Docker/Compose 配置、只读验收 Artifact | 本地重建并重启 8750；health、进程、端口、卷、lease、20 行日志；不调用模型 | 待复核 |

## 3. 通用验收门槛

每个根因只有同时满足以下条件才能标为“已闭环”：

1. 生产入口和不可绕过的运行时约束已经实现。
2. 旧 SQLite 能 additive-compatible 打开，既有历史不被重置。
3. 至少一个最小反例测试先证明旧行为错误，再证明新行为正确。
4. 针对性测试通过，随后相关测试模块通过。
5. `git diff --check` 对源码和测试通过；未跟踪审计报告保持原样。
6. 证据记录实际文件、SHA-256、revision、命令和结果；不以模型自述、心跳或退出码单独判定完成。

最终只有在 19 个根因全部闭环、完整回归通过、Docker 8750 获准验收完成，并重新复核 168 条无未解释冲突时，才允许结束 Goal。

## 4. 已闭环证据

### RC-P0-01

- 生产约束：`_create_task` 只创建 `planner_intake`；`instruction_facts` 不输出 `size` 或授权结论；只有结构化 `PlannerResult.classification` 可以选择 simple/medium/large。
- 运行边界：Planner HostJob 使用 Host Bridge 私有 `planner-workspaces/<workspace_key>`，强制 `read`，不要求项目 host path，也不能把 Provider 写入带回基础 workspace。
- 结果约束：simple/medium 必须恰好一个 Task；large 必须至少两个方案和 2-50 个 DAG Task；无语义理由、信息不足合同错误、非布尔 owner choice 均 fail closed。
- 针对性验证：`PYTHONDONTWRITEBYTECODE=1 /opt/homebrew/bin/python3.13 -m unittest -v` 后跟 8 个 Planner 生命周期用例和 1 个 Host Bridge workspace 用例，`Ran 9 tests ... OK`。
- 相关回归：`PYTHONDONTWRITEBYTECODE=1 /opt/homebrew/bin/python3.13 -m unittest tests.test_vertical_slice tests.test_host_bridge`，`Ran 54 tests ... OK (skipped=1)`。
- 完整回归：`PYTHONDONTWRITEBYTECODE=1 /opt/homebrew/bin/python3.13 -m unittest discover -s tests`，`Ran 69 tests ... OK (skipped=1)`。
- 未调用真实模型、外部 Provider 或网络；以上 Planner 运行证据由结构化本地 test double 产生，Host Bridge 测试使用本地假可执行文件。
- P-14/P-15 复核补强：Planner、Worker 和 Checker Prompt 都禁止直接向主人提问；信息不足或 Checker 阻塞只能返回结构化 `blocking_reason`/`NEEDS_DECISION + decision_reason`，由 lifecycle 归约后交 Butler。`_ensure_project_question` 在同一 `BEGIN IMMEDIATE` 事务中跨项目选择一个问题，只有一个 question 可为 `waiting=true`；当前问题解决后自动把下一项转为 waiting，历史消息原样保留并只补 resolution 元数据。
- 反例验证：`test_only_one_owner_question_waits_across_projects` 证明两个项目同时 `needs_decision` 时只出现一个等待问题，第一项解决后第二项才被呈现；与既有 `test_high_risk_plan_asks_exactly_one_project_butler_question` 一起运行，`Ran 2 tests ... OK`。

### RC-P0-02

- 升级合同：`classification.upgrade_path` 只能是 `simple`、`simple→medium` 或 `simple→medium→large`，每一级必须有结构化 bounded facts；选中 Plan 将该分类和升级证据一起版本化持久化。
- 客观选择：每个 Plan 明确 `required_coverage` 和 `selection`；每个备选方案提交相同覆盖、整数 effort/risk 指标。large 自动选择必须由代码证明选中方案对其他方案客观占优；`confidence=0.99` 不能越过该 gate，真实取舍走 `owner_required`。
- 原子 TaskSpec：每个 Task 强制一个安全 component 和完整 deliverable；large component 唯一；每个 coverage id 只能由一个 Task 拥有，所有 Task 结果并集必须精确覆盖 Plan。
- 完整合同：`spec_json.task_contract` 固化 inputs、result type/coverage/Artifact path+format、acceptance、独立 Checker 映射、depends_on、`max_runtime_seconds` 和 authorization boundary。依赖 Task 必须逐一声明 `task_result` 输入；large 每 Task runtime 必填并冻结进角色 settings。
- 针对性验证：`test_large_plan_requires_objective_atomic_complete_task_contracts` 覆盖缺 result、缺 runtime、重复责任、覆盖重叠、自报高置信但不占优、无升级事实六类反例；Planner/DAG 六用例通过。
- 相关回归：`PYTHONDONTWRITEBYTECODE=1 /opt/homebrew/bin/python3.13 -m unittest tests.test_vertical_slice`，`Ran 46 tests ... OK`。
- 完整回归与格式：`git diff --check` 通过；`PYTHONDONTWRITEBYTECODE=1 /opt/homebrew/bin/python3.13 -m unittest discover -s tests`，`Ran 70 tests ... OK (skipped=1)`。

### RC-P0-03

- 唯一写入权：`advance_project` 是正常运行时唯一打开 `advance_project` 写作用域的入口；`write_task_fields` 和 `increment_task_retry` 在该作用域外 fail closed。schema 初始化只能调用专用 legacy repair，不能借 migration 作用域写任意 Task 字段。
- 写入归并：execution、verification、Provider 预算和 Cronner checkpoint 不再各自执行 Goal/Task SQL。Provider 的 `record_model_call` 只持久化原始/归一化 usage 并返回 `model_budget_fact`；生命周期归约器统一决定是否进入 `needs_decision`。
- 不可绕过约束：静态边界测试扫描全部生产 Python 文件，拒绝 lifecycle 模块外出现 Goal/Task 的 INSERT/UPDATE/DELETE SQL，也拒绝 lifecycle/store 之外打开 lifecycle write scope；动态测试证明直接调用 Task writer 会抛错且数据库不变。
- 针对性验证：`PYTHONDONTWRITEBYTECODE=1 /opt/homebrew/bin/python3.13 -m unittest -v tests.test_review_fixes tests.test_lifecycle_ownership`，`Ran 12 tests ... OK`。
- 相关回归：`PYTHONDONTWRITEBYTECODE=1 /opt/homebrew/bin/python3.13 -m unittest -v tests.test_vertical_slice tests.test_host_bridge`，`Ran 55 tests ... OK (skipped=1)`。
- 完整回归与格式：`git diff --check` 通过；`PYTHONDONTWRITEBYTECODE=1 /opt/homebrew/bin/python3.13 -m unittest discover -s tests -v`，`Ran 72 tests ... OK (skipped=1)`。

### RC-P0-04

- 完整输出：Host Bridge output 支持从 offset 0 分块读取到 EOF，正式 Planner、Worker、Checker 与 probe 消费均显式要求 `complete=True`；tail 只保留为观察字段并标记 `tail_observation_only`。
- Artifact 真源：SQLite schema additive 升级到 v7，为 Artifact 增加 `scope_json` 与 `source_task_id`；所有正式 Artifact 统一登记 path、SHA-256、bytes、revision、scope、source Task 与 coverage。旧历史行只做兼容回填，不删除或重写历史业务数据。
- 下游完整消费：Checker 读取正式 execution manifest 和其中全部 result entries；依赖上下文不再只取前两个依赖，必须逐个验证终态、完整 Artifact/Evidence 以及 path/hash/revision/scope/source 血缘。
- 审查后修复：Planner 对 `audit_then_repair` 工作流要求单一完整审查 Artifact 根节点，后续修复 Task 必须传递依赖该审查结果；末尾若干行不能作为正式输入。
- 反例验证：`tests.test_artifact_contract` 使用 3 个依赖和超过 1 MiB 的 Artifact 验证完整消费，并逐一篡改 path/hash/revision/source 后确认 fail closed；Host Bridge 测试验证超过 1 MiB stream 分块到 EOF。
- 完整回归：`PYTHONDONTWRITEBYTECODE=1 /opt/homebrew/bin/python3.13 -m unittest discover -s tests -v`，`Ran 79 tests ... OK (skipped=1)`。

### RC-P0-05

- Planner Checker：Planner 结构化结果先写入完整 `planner.json` Artifact；独立 Checker 只收到冻结 TaskSpec、Artifact path/hash/revision 与完整结构化内容。只有 PASS 后 lifecycle 才能安装 Plan；CHANGES_REQUIRED 直接进入 `needs_decision`。
- 递补边界：Planner Checker Provider 被拒绝或失败时，复用同一已哈希 Planner Artifact，在相同 Checker TaskSession 内创建下一代 SessionGeneration 和新 Checker HostJob；不会重新调用 Planner，也不会创建第二套 Plan 状态机。
- Worker Checker：正式 Worker 的 Checker 只读取 TaskSpec、正式 Artifact/diff/Evidence、workspace snapshot 与必要 handoff，不读取 Worker 自由聊天；每一项正式 result entry 都做血缘校验。
- 模型 probe Checker：0 Token probe 未调用模型，继续走确定性合同；minimal probe 调用模型后改用 `independent_checker`，Checker 校验完整 probe Artifact、Provider 身份、terminal marker 和 Token cap，PASS Evidence 与源 Artifact 通过 path/hash/revision/source 绑定后才能完成 Task。
- 反例验证：`test_planner_checker_rejection_blocks_plan_installation`、`test_planner_checker_provider_fallback_reuses_checked_artifact` 和 `test_provider_probe_tasks_record_zero_and_minimal_token_evidence` 分别覆盖拒绝、Provider 递补和模型 probe 血缘。
- 相关回归：`/opt/homebrew/bin/python3.13 -m unittest tests.test_vertical_slice`，`Ran 49 tests ... OK`；完整回归同上，`Ran 79 tests ... OK (skipped=1)`。测试使用本地结构化 test double，没有调用真实模型、外部 Provider 或网络。

### RC-P0-06

- 单一终态门禁：新增 `finalize_task_terminal`；任何 `done`/`cancelled` 写入前都会查询全部 HostJob，存在 `dispatching/running/cancelling` 即 fail closed。终态同时归档所有 SessionGeneration，并写 `temporary_capabilities_revoked` 事件。
- additive schema：SQLite schema v8 给 Task 增加 `terminal_capabilities_revoked_at`。旧库通过 `ALTER TABLE ADD COLUMN` 升级，既有 Task、Artifact、Evidence、日志和 handoff 均保留。
- 全类型停止：cancel 不再只处理 `provider_task`，会给该 Task 的所有 active HostJob 写 stop request。provider、Checker、Planner、minimal probe 和 Git publish 均通过 Host Bridge reconcile/cancel；若存在多个 active job，逐个终结，最后一个终结前 Task outcome 保持 NULL。
- 授权与引用失效：终态只保存 authorization 和 opaque Secret reference 的 SHA-256，不把引用写进撤销事件；取消后 rerun 会删除旧 `authorization`、`secret_ref(s)` 和 `credential_ref(s)` 并提高 TaskSpec revision。Git runtime 同时检查授权所属 Task 尚未写 `terminal_capabilities_revoked_at`。
- 保留历史：取消只改变生命周期、HostJob 和 capability 有效性，不删除 Task、workspace、Artifact、Evidence、log 或 handoff，符合 S-05。
- 反例验证：`test_active_model_probe_stops_before_cancelled_terminal_state`、`test_active_git_publish_stops_and_revokes_authorization_before_cancel`、`test_terminal_revocation_is_explicit_and_rerun_drops_old_refs` 覆盖活动模型探针、活动外部 Git 动作和旧引用 rerun。
- 完整回归：`PYTHONDONTWRITEBYTECODE=1 /opt/homebrew/bin/python3.13 -m unittest discover -s tests`，`Ran 82 tests ... OK (skipped=1)`；未调用真实模型或外部 Provider。

### RC-P1-01

- Bridge 只报事实：owned process 和 restart 后 orphan watchdog 达到 `timeout_seconds` 时只持久化 `deadline_reached_at`，不再自动 SIGTERM、固定等待 5 秒或把 timeout 伪造成 returncode 124。
- 生命周期顺序：Task deadline 先进入 `timeout_reconcile`，HostJob 保持 running；下一步读取完整 output refs 和 Provider 状态，写 timeout log Artifact 与结构化 Evidence。Cronner 在该步结束后生成 Warm handoff，随后下一 tick 才发 graceful stop。
- 冻结 grace：首次 stop 明确 `force=False` 并记录 `cancel_sent_at`；到 TaskSession 冻结的 `stop_grace_seconds` 前只 poll，超出后才调用 `force=True`，由 Host Bridge 发 SIGKILL。没有固定 5 秒常量。
- 晚到真实结果：reconcile 时若 HostJob 已 terminal，清除 Task deadline，保存完整正式结果，并按真实 returncode/result contract 进入 verify/recover/fallback/decision；不会发送 stop，也不把 timeout 本身当 Task 失败。
- 写操作安全：由 lifecycle 发出的强制停止走 deadline recovery/needs-decision，不进入 Provider 自动递补；只有 reconcile 得到的真实 terminal 结果才进入正常结果决策。
- 反例验证：`test_owned_process_deadline_is_fact_not_automatic_kill`、`test_restart_watchdog_reports_deadline_without_killing`、`test_deadline_reconciles_and_gracefully_stops_active_host_job`、`test_deadline_reconcile_accepts_late_terminal_result_without_stop`。
- 相关回归：`/opt/homebrew/bin/python3.13 -m unittest tests.test_host_bridge tests.test_review_fixes tests.test_vertical_slice`，`Ran 73 tests ... OK (skipped=1)`；全部为本地假进程/结构化事实，未调用 Provider。

### RC-P1-02

- Generation 门禁：删除基于 `rotation_input_tokens` 的健康 Session 自动换代。Provider 原生 compact 只登记 `provider_compacted` 事实并继续同一物理 Session；只有冻结 Provider 顺序中的真实递补或同 Provider 不可恢复重试才创建下一代。
- 最新 Warm：新增 `checkpoint_task_session`。创建替代 generation 前，先把当前 generation 的全部新 HostJob 写入只增 Cold segment，再生成带 revision 的历史 handoff，按 path/SHA-256/bytes/source Task 重新读取校验；只有校验成功才归档旧 generation。递补事件保存实际 bootstrap handoff 的 path、SHA-256 和 revision。
- append-only Cold：Host Bridge 不再在终态读取并覆写正式 stream。新增本地 `stream_capture.py` 包装进程，Worker stdout/stderr 在产生时逐记录脱敏并只追加到 `*.segment-000001.log`；包装进程与 Worker 同进程组，Bridge 重启后仍可继续落盘和被收敛。终态只解析 usage、收紧权限和写状态，不改变 stream inode、bytes 或内容。
- 预算冲突：每个 TaskSession 在安装 Plan 时冻结 `budget_contract`，同时保存 context/handoff/checkpoint/segment/observation 的有效值和来源；强制 TaskSpec+acceptance 超过 Hot cap 立即拒绝，Warm/Context 和 observation/segment 的可能冲突形成显式 warnings，不静默丢掉强制内容。
- 反例验证：`test_complete_output_is_chunked_to_eof_and_never_tail_overwritten`、`test_bridge_terminal_streams_are_private_redacted_and_complete`、`test_planner_checker_provider_fallback_reuses_checked_artifact`、`test_context_policy_compaction_event_and_non_native_rotation`、`test_hot_warm_cold_continuity_is_bounded_and_append_only`。
- 相关回归：`/opt/homebrew/bin/python3.13 -m unittest tests.test_host_bridge tests.test_review_fixes tests.test_vertical_slice`，`Ran 73 tests ... OK (skipped=1)`；全部使用本地假进程和结构化 test double，未调用真实模型、外部 Provider或网络。

### RC-P1-03

- adapter 语义：Host Bridge 为 Codex/Cursor 明确返回 `usage_kind=cumulative`，为具有单次 API usage 合同的 JSON Worker 返回 `single`，同时接受 Provider 结果中显式且受控的 `usage_kind`。Planner、Worker、Checker 和 minimal probe 不再硬编码 `single`，而是持久化 adapter 原始语义。
- 物理 Session：SQLite additive 升级到 schema v9，`model_calls` 新增 `physical_session_id`。新调用使用 `provider_key + external_session_id`，尚未取得外部 id 时使用当前 TaskSessionGeneration 的稳定 id；旧行通过 generation 对应的 external session 安全回填，不删除或改写历史 usage。
- 归一化：累计 snapshot 原值继续保存在 input/cached/output 列；预算和监控只聚合同一 `physical_session_id + provider` 的相邻差值。新的物理 Session 即使留在同一逻辑 generation 也会重置累计基线，避免错误地用负差值或重复累计。
- Token 纪律：`cached_input_tokens` 继续强制为 input 子集，未 cached input 的累计值也不得倒退；Token 监控同时按 project/task/model/Worker/TaskSession 展示物理 Session delta。
- 反例验证：`test_cursor_read_mode_and_cumulative_token_normalization`、`test_provider_facts_and_token_normalization_are_fail_closed`、`test_schema_v9_preserves_history_and_adds_terminal_revocation`；覆盖同一物理 Session 相邻快照、物理 Session 改变后基线重置、cached/uncached 倒退拒绝和旧库回填。
- 完整回归：`/opt/homebrew/bin/python3.13 -m unittest discover -s tests`，`Ran 84 tests ... OK (skipped=1)`；没有调用真实模型、外部 Provider 或网络。

### RC-P1-04

- 单一策略：新增 `secret_policy.py`，统一识别已知 Token 前缀、Bearer、Secret/Key/Password 赋值、签名 URL 和私钥头；统一日志脱敏。合法跨边界输入只能携带 `secret-ref://...` 或 `credential-ref://...` opaque reference。
- 入库门禁：global/project message、idempotency key、项目元数据、owner action/plan、project setting、project rule 和 Butler 搜索在写 SQLite/Library 前统一 fail closed。重复请求也先检查 Secret，不能通过 idempotency 快路绕过。
- Prompt 门禁：Host Bridge 在启动任何 Worker/Planner/Checker 前再次检查完整 Prompt；即使绕过 Web/API intake 直接请求 Bridge，明文 Secret 仍返回 400 且不创建进程或状态记录。
- 日志与报告：Host Bridge Cold stream、Provider session log 和 Provider report 共用同一脱敏实现；正式 stream 在产生时脱敏后只追加，不存在“先持久化明文、终态再覆盖”的窗口。opaque reference 保留原文，便于授权边界追溯但不是凭证。
- 生命周期：Task 终态继续由 schema v8+ 的 `terminal_capabilities_revoked_at` 和 `temporary_capabilities_revoked` 统一使 authorization/Secret reference 失效；rerun 会剥离旧 `secret_ref(s)`/`credential_ref(s)`，不能复用终态引用。
- 反例验证：`test_plaintext_secrets_are_rejected_before_persistence_or_prompt` 覆盖 message/global/action/rule、SQLite/WAL/data-root 扫描和 opaque ref；`test_restricted_durable_job_and_restart_recovery` 覆盖直接 Bridge Prompt 拒绝；`test_bridge_terminal_streams_are_private_redacted_and_complete` 覆盖 append-only 日志脱敏。
- 完整回归：`/opt/homebrew/bin/python3.13 -m unittest discover -s tests`，`Ran 85 tests ... OK (skipped=1)`；未调用真实模型、外部 Provider 或网络。

### RC-P1-05

- 三档 capability：每个 ProviderStep 都携带 `read_only`、`recoverable_workspace_write` 或 `authorized_external_effect`，并冻结允许动作、作用域、TaskSpec revision 和有效期。Host Bridge 在进程创建前重新校验 capability 与 adapter/access 的组合，不接受缺失、越级、过期或 Task/Project/target 不匹配的授权。
- 可恢复写：除专用 Git executor 外，Provider 写任务只在一次性 staging workspace 运行。成功后由控制面按白名单路径应用结果；删除不直接 unlink 历史文件，而是把原文件改名为带 UTC 时间戳的 `.rm.YYYYMMDDHHMMSS`。symlink 逃逸、`.git`、Secret/env 和缓存文件都 fail closed。
- 外部副作用：`authorized_external_effect` 只允许已有专用 executor 支持的 Git 发布/强制租约动作，并继续校验项目、Task、TaskSpec revision、目标、动作和 expiry。自然语言 Task 若要求部署、生产切流、付款、权限或生产迁移且没有专用 executor，Planner 在安装 Plan 前拒绝，而不是让通用 Worker 自由执行。
- 反例验证：`test_recoverable_workspace_delete_is_renamed_with_timestamp`、`test_external_effect_capability_is_scoped_and_expires`、`test_missing_capability_is_rejected_before_process_start`、`test_unsupported_external_effect_is_rejected_during_planning`。
- 相关回归：`/opt/homebrew/bin/python3.13 -m unittest tests.test_host_bridge tests.test_review_fixes tests.test_vertical_slice`，`Ran 76 tests ... OK (skipped=1)`；完整回归 `Ran 87 tests ... OK (skipped=1)`。全部使用本地假进程/结构化事实，未调用真实模型、外部 Provider 或网络。

### RC-P1-06

- 删除自动晋升：删除 `verification PASS → worker_template` 生产函数和确定性/模型 Checker 两个调用点。Checker PASS 只形成验证 Evidence 和生命周期事实，不再静默扩大项目级长期约定。
- 显式长期约定：项目规则仍只能通过已有 owner action/plan 明确写入，并由 role snapshot 消费；它与普通 Task PASS 的临时执行经验分离，不创建第二套模板状态机。
- 反例验证：端到端自动完成和修复重试的严格事件序列均不再出现 `worker_template_promoted`，普通 PASS 后 `kind=worker_template` 的项目规则为空；生产代码与测试中也没有残留晋升符号。
- 相关回归：`/opt/homebrew/bin/python3.13 -m unittest tests.test_host_bridge tests.test_review_fixes tests.test_vertical_slice`，`Ran 76 tests ... OK (skipped=1)`；完整回归 `Ran 87 tests ... OK (skipped=1)`。

### RC-P2-01 / RC-P3-02

- 四类产品页：主导航和实际顶层 view 都收敛为全局首页、项目详情、Task 详情、设置与资源库；项目管家并入项目详情，Token/Monitor 作为设置页中的只读观察面，不保留额外产品页。
- 安全完整文件入口：Task snapshot 为 Artifact、Evidence、Handoff、Session segment 生成 `Task ID + opaque file_id` URL；后端从 Task 权威索引重新解析受控路径，并在返回完整 bytes 前复核 SHA-256 和 revision。浏览器不能提交本地 path；伪造 file_id 返回 404。
- 详情字段：Task 页同时展示 role/Checker、Provider/model、Worker、TaskSession、物理 Session、generation、Token、HostJob、Artifact/Evidence/Handoff 的 path/SHA/revision 和完整 Session segment 入口。模型名来自最新 ModelCall 事实，不再对非本地 Session 显示空占位。
- 观察标签：API 字段改为 `observation_tail` 并返回不可作为 Artifact/Evidence/完成依据的声明；UI 同样把默认 20 行标为有界观察。
- 反例验证：四页数量、完整文件 200、伪造 ID 404、响应 SHA header、相对路径边界和观察标签均由 `test_http_intake_decision_and_automatic_completion`、`test_message_to_verified_done` 覆盖。

### RC-P2-02

- 设置真源：Global/Project/Task+Role 新增 `provider_models`，与 `provider_order` 一起进入 SQLite 并在 TaskSession 创建时冻结实际值和来源。dispatch 从冻结 settings 解析 model，经 ProviderStep 传给 Bridge；HostJob 记录 requested/actual model。
- 环境边界：Bridge 私有 env 和安全子进程 env 均拒绝 `DEEPSEEK_MODEL`/`KIMI_MODEL`，只保留部署/地址、opaque credential 和运行目录项。Codex、Cursor、JSON Worker 的显式模型选择统一转成受控 CLI 参数；`default` 也是 SQLite 中可追溯的显式选择。
- 反例验证：非法模型名拒绝；项目模型设置冻结来源；私有 env 中模型键拒绝；宿主环境中的模型键不会进入子进程；Bridge HostJob 保留 requested model。

### RC-P2-03

- 显式入口：普通 PASS 不自动登记脚本。只有主人对当前 done Task 的明确 `promote_script` action，才能选择该 Task 当前 revision 的正式 output Artifact 和资源键。
- 双重门禁：intake 与 lifecycle 都复核 Task 终态、TaskSpec `script_contract`、Artifact ID/path/SHA/revision/source、当前完整 Checker Evidence。Python 文件还必须是 UTF-8 单文件、含独立 public callable、`main` CLI 和 `SystemExit(main())`；TaskSpec 必须声明包含 0/非 0 的 exit codes、stdout/stderr 合同及三项不同 acceptance。
- additive lineage：schema v10 只增 `library_items.source_task_id/source_artifact_id/source_revision`，脚本正文复制为版本化 library 文件，SQLite 仍只保存索引、revision、SHA 与来源。
- 反例验证：无合同、Evidence 不完整、缺 CLI guard 均拒绝；通过后脚本 bytes/SHA 与源 Artifact 一致且 lineage 完整。

### RC-P2-04

- 先精确后语义：`semantic_search` 总是先调用只读 SQLite/file index；只要当前项目有精确结果，就返回引用并明确 `model_queued=false`，不创建模型调用或查询 Task。
- 显式且受控：只有用户在零命中后点击“受控语义归纳”，才把最多 50 个稳定 `kind/ref/detail` 来源排入现有 Planner→Worker→independent Checker 主线。查询 Task 明确 `workspace_change_required=false`，只能使用 Codex/Cursor read adapter 和 disposable planner workspace，不能写业务 workspace。
- 来源与验收：TaskSpec 固定 `semantic_summary_sources`、`semantic_summary_scope` 两项 acceptance；Worker 只能消费 Prompt 中的规范化来源引用，正式完整摘要由 provider report Artifact 承载，独立 Checker 复核全部来源和只读范围后才能完成。
- 反例验证：精确命中前后 Task 数不变；模糊归纳先经过 Planner Checker，随后安装 read-only TaskSpec、独立 Checker、无 external capability，且没有第二队列/状态机。
- P2 相关回归：`/opt/homebrew/bin/python3.13 -m unittest tests.test_host_bridge tests.test_review_fixes tests.test_vertical_slice`，`Ran 78 tests ... OK (skipped=1)`；全为本地 test double/确定性进程，未调用真实模型、外部 Provider 或网络。

### RC-P3-01

- macOS 契约：`/opt/homebrew/bin/python3.13 -m unittest -v tests.test_host_bridge`，`Ran 12 tests ... OK (skipped=1)`；11 项 start/read/stop、只增 segment、Session continuity、capability、usage 与 read-only workspace 契约通过。唯一跳过项仅因当前桌面沙箱不能取得进程 start identity。
- Linux 契约：`docker run --rm --network none -v /Users/niugengtian/work/plow-whip-web_blue-1:/src:ro -w /src python:3.13-slim python -m unittest -v tests.test_host_bridge`，`Ran 12 tests ... OK`；Linux 下同一套契约 12/12 通过，包括 macOS 沙箱跳过的 Bridge restart 后进程 reconcile/cancel。
- 隔离边界：Linux 验证使用只读源码挂载、`--network none` 和本地已缓存 `python:3.13-slim`；没有启动真实 Provider、没有读取生产 Secret，也没有修改 8750 数据卷。
- 完整回归与格式：`/opt/homebrew/bin/python3.13 -m unittest discover -s tests`，`Ran 89 tests ... OK (skipped=1)`；`git diff --check` 通过。
