# PlowWhip Web 基线修复台账（对照已冻结 V3）

## 1. 权威输入与边界

- **唯一基线（V3，已冻结 2026-07-26）**：`docs/MINIMAL_REDESIGN_BASELINE_V3.zh-CN.md`
- V3 冻结 SHA-256：`0ffd3f63fbe181696043fb52512e33abc0c6981523c90043d9d3bd2cfe835292`
- 历史基线（V2，仅对照/废止溯源，不作符合性依据）：`/Users/niugengtian/work/plow-whip-web-v2/docs/MINIMAL_REDESIGN_BASELINE_V2.zh-CN.md`
- 基线 SHA-256（V2）：`4ae79ba906997640304a202ba0d453e2437e77e36468c29eb4a185ad970f6098`
- V3 相对 V2 已裁定：去 Plan A/B；Planner 权威分层（R1）；Checker 分层（R2）；NeedsDecision 收窄且结构化点选（R3/ND-*）；目标序 G-01>G-02>G-03；禁止章节 PASS / 自由文本改写 TaskSpec 等降质旁路。
- V3 2026-07-26 整链反审补齐（对齐三目标，禁「提醒一条补一条」）：脚本模块管线 D-32～D-34；Provider 默认 `cursor_cli→deepseek`（B-04'）+ 管理面 B-27；废止双基线「沿用 V2」；A-16/完成门槛对齐合同 Checker；废止 LIVE-DS-21 章节覆盖字面；RC-P2-01/03 与实现冲突项改待复核。
- V3 已升格台账机制（防进行中误杀与同构重烧）：LIVE-DS-22/23、MECH-01/02/04/05/06/07 → A-27～A-33、T-11～T-16、R-18～R-20（见 V3 正文；改后须重算 V3 SHA）。
- 差距报告：`docs/BASELINE_V2_CONFORMANCE_AUDIT.zh-CN.md`
- 差距报告 SHA-256：`9b553d741728dc1c5e843148ac28e76efc4a461ac6d74a05d060e77038c72cd8`
- 修复起点：`main@942f246d7b82dcfdcb74452c654646185a27067f`
- 起点 tree：`4b403873193070c374703129a75b28bbd0e53c26`
- 起点工作区：只有未跟踪的差距报告；必须原样保留。
- 起点回归：Python 3.13 下 `67 tests OK, skipped=1`。
- 现场边界：Docker 服务使用端口 `8750`。源码验证可按需重建和重启该容器，但不得调用模型或外部 Provider。
- 兼容边界：SQLite 只允许 additive-compatible 变化；不新增重复状态机、重复队列或第二套运行时真源。

状态枚举：`待复核 / 进行中 / 已实现待回归 / 已闭环 / 受外部环境限制`。

### 台账状态刷新规则（2026-07-26 · 对齐 V3）

| 状态 | 含义 |
|---|---|
| 已闭环 | 实现 + 反例测试 +（若适用）现场证据均满足**当时条款**；若 V3 改写了条款，必须改回「待复核」按 V3 重验 |
| 已实现待回归 | 代码与定向测试已有；缺干净现场 E2E 或未按 V3 条款重跑 |
| 进行中 | 实现未完成或关键路径仍缺口 |
| 待复核 | 曾标闭环/完成，但与 V3 或独立审计冲突，禁止继续当完成证据 |
| 受外部环境限制 | 仓库外依赖（PATH/LaunchAgent/密钥）阻塞 |

**本次纠偏要点**：`RC-P0-03` 不得再标已闭环（仍有第二写入口）；V3 已升格的进行中判断/超时/同构重试等，实现侧多数为「已实现待回归」，不等于 DeepSeek 无人值守 E2E 已证明。

### V3 Wave1–3 落地记录（2026-07-26 · blue）

| Wave | Commit | 摘要 | 证明边界 |
|---|---|---|---|
| Wave1 | `1393c35` | A-16a 禁章节覆盖语义拒；废止 A/B+0.95；`decision_options`/`select_option`；failure-signature 分桶 | 定向 unittest；**非**付费 E2E |
| Wave2 | `2b23060` | T-14 增量续命；checkpoint 事实化；Bridge 分类+/health；force-cancel 收敛；exploration turn advisory；`script_library_search` | 定向 unittest；**非**付费 E2E |
| Wave3 | 见本推送 tip | Provider×条款矩阵回归 + 台账重标；禁把单厂商 Done 当全厂闭环 | `tests/test_v3_provider_matrix.py` + `docs/runtime-audits/V3_WAVE3_PROVIDER_MATRIX_20260726.md`；现场 cursor/deepseek/kimi E2E **仍待主人密钥环境** |

## 2. 根因修复顺序

| ID | 优先级 | 合并根因 | 条款 | 当前生产路径 | 预计修改文件 | 最小验证 | 状态 |
|---|---|---|---|---|---|---|---|
| RC-P0-01 | P0 | 所有正式指令统一进入模型 Planner，启发式仅为输入事实；角色阻塞只经 Butler 串行呈现 | P-01/P-02/P-05/P-06/P-14/P-15/C-01 | `POST /api/messages → intake/butler → lifecycle._create_task → planner` | `plowwhip/planner.py`、`plowwhip/lifecycle.py`、`plowwhip/execution.py`、`plowwhip/verification.py`、`plowwhip/provider.py`、`plowwhip/host_bridge.py`、`tests/test_vertical_slice.py`、`tests/test_host_bridge.py` | simple/medium/large 三类都先形成 Planner HostJob 与结构化 PlannerResult；静态断言 regex/kind 不决定最终 size；跨项目仅一个 `waiting=true` Butler question | 已闭环 |
| RC-P0-02 | P0 | large 方案客观选择、语义原子 TaskSpec 与有依据升级 | V3:P-09'/P-10/P-11'/…（**废止强制 A/B**） | `planner_prompt/parse/normalize_plan → lifecycle._materialize_plan/_install_plan` | `plowwhip/planner.py`、`plowwhip/lifecycle.py`、`tests/test_vertical_slice.py` | 按 V3：单一 Plan + 原子合同；不再要求 Plan A/B / 0.95 自动选 | 已实现待回归 |
| RC-P0-03 | P0 | lifecycle 成为 Goal/Task 生命周期字段唯一写入者 | V3:L-01/L-03' | `advance_project` 调用 execution/verification/provider/cronner | `plowwhip/lifecycle.py`、`plowwhip/execution.py`、`plowwhip/verification.py`、`plowwhip/provider.py`、`plowwhip/cronner.py`、`plowwhip/store.py`、测试 | 禁止 lifecycle 外写；`record_checkpoint_failure` / execution·verification 直接写必须归并 | 已实现待回归 |
| RC-P0-04 | P0 | 完整结果、Artifact manifest 与完整下游输入 | A-23/R-01～R-08/R-12/R-14～R-16/D-06/D-11/D-13 | `execution finalize → artifacts → continuity dependencies → verification manifest` | `plowwhip/execution.py`、`plowwhip/continuity.py`、`plowwhip/verification.py`、`plowwhip/store.py`、测试 | 超过 1 MiB 的交付物完整读取；任一 path/hash/revision/scope/source 篡改均 fail closed；tail 不进入输入 | 已闭环 |
| RC-P0-05 | P0 | 所有模型产出统一由独立 Checker 验收 | V3:A-16'/A-16a（合同/语义分层） | Planner/Worker/probe 模型 HostJob → 正式结果 → Checker | `plowwhip/lifecycle.py`、`plowwhip/execution.py`、`plowwhip/verification.py`、测试 | 按 V3 分层验收；禁章节覆盖结构化否决；salvage 必经合同 Checker | 已实现待回归 |
| RC-P0-06 | P0 | Task 终态前收敛 HostJob、授权和 Secret 引用 | B-21/S-05 | `cancel/complete action → active HostJob → terminal outcome` | `plowwhip/lifecycle.py`、`plowwhip/execution.py`、`plowwhip/verification.py`、测试 | provider_probe/git_publish 取消时先 stop/reconcile，随后撤销引用，最后写 cancelled/done | 已闭环 |
| RC-P1-01 | P1 | timeout 顺序与冻结 grace | T-02/T-06/T-07/T-09 | Host Bridge deadline → lifecycle reconcile/checkpoint/stop | `plowwhip/host_bridge.py`、`plowwhip/lifecycle.py`、`plowwhip/execution.py`、测试 | deadline、grace、SIGTERM/SIGKILL、晚到结果逐步断言 | 已闭环 |
| RC-P1-02 | P1 | Generation、Warm handoff 与 append-only Cold 无损恢复 | A-14/M-03/M-06/M-09/D-11 | Provider failure/compact → continuity checkpoint → generation replacement | `plowwhip/execution.py`、`plowwhip/continuity.py`、`plowwhip/host_bridge.py`、测试 | 健康 Session 不轮换；替换前校验最新 Warm；Cold segment 不覆盖 | 已闭环 |
| RC-P1-03 | P1 | single/cumulative Token 按物理 Session 归一化 | M-11/M-12/M-13 | Host Bridge usage → adapter fact → model_calls → monitor | `plowwhip/host_bridge.py`、`plowwhip/provider.py`、`plowwhip/execution.py`、`plowwhip/lifecycle.py`、`plowwhip/verification.py`、测试 | 同 generation 累计快照取相邻差值；跨 generation 重置基线 | 已闭环 |
| RC-P1-04 | P1 | Secret 不进入 SQLite/Prompt/模板/日志并在终态失效 | D-30/B-21 | message/action/library/provider env → persistence/prompt/log | `plowwhip/intake.py`、`plowwhip/butler.py`、`plowwhip/lifecycle.py`、`plowwhip/host_bridge.py`、`plowwhip/store.py`、测试 | 注入测试 Secret，扫描 SQLite、Artifact、Prompt、模板和日志均无明文 | 已闭环 |
| RC-P1-05 | P1 | 三档 capability、scope、expiry 运行时硬校验 | D-20/B-17/B-18/B-19 | TaskSpec authorization → execution/Host Bridge | `plowwhip/planner.py`、`plowwhip/execution.py`、`plowwhip/host_bridge.py`、测试 | 未授权不可逆/外部动作 fail closed；删除改为时间戳 `.rm.*` | 已闭环 |
| RC-P1-06 | P1 | 禁止 PASS 自动污染 WorkerTemplate | D-21/D-22 | `verification PASS → project worker_template` | `plowwhip/verification.py`、`plowwhip/lifecycle.py`、测试 | 普通 PASS 不创建模板 revision；显式长期 owner decision 才更新 | 已闭环 |
| RC-P2-01 | P2 | 主导航含独立 Token/Monitor + Task-ID 完整文件入口与观察标签 | V3:B-25/B-26/B-13/B-14/D-15 | UI navigation → GET task/file → monitor references | `plowwhip/app.py`、`plowwhip/monitor.py`、`plowwhip/ui.py`、测试 | **六导航**（全局/项目/Task/Token/Monitor/设置）；禁把 Token/Monitor 埋设置；ID/hash/tail 合同仍成立 | 已实现待回归 |
| RC-P2-02 | P2 | Provider/model 业务选择移出环境变量 | D-28/D-29 | SQLite settings snapshot → Host Bridge dispatch | `plowwhip/store.py`、`plowwhip/execution.py`、`plowwhip/provider.py`、`plowwhip/host_bridge.py`、测试 | env 不接受业务模型选择；冻结 settings 决定 model | 已闭环 |
| RC-P2-03 | P2 | 脚本验收后门禁入库（显式或 auto enqueue 同门） | V3:D-24'/D-32～D-34 | done/PASS → promote_script（owner 或 auto）→ library_items | `script_library.py`、`verification.py`、`lifecycle.py`、测试 | 未 PASS/缺合同/非单文件 CLI 拒绝；auto 与显式走同一 AST/Evidence/SHA；已复用模块不重复晋升 | 已实现待回归 |
| RC-P2-04 | P2 | Butler 精确检索不足时的受控语义归纳 | B-12 | global message → exact SQLite/file search → read-only semantic summary → route | `plowwhip/butler.py`、`plowwhip/lifecycle.py`、`plowwhip/verification.py`、测试 | 精确查询零模型；模糊查询有来源引用、独立验收且不创建业务 Task | 已闭环 |
| RC-P3-01 | P3 | Host Bridge macOS/Linux 契约证据 | B-02 | start/read/stop/resume/segment | `plowwhip/host_bridge.py`、`tests/test_host_bridge.py`、验收 Artifact | 本机 macOS 确定性契约；Linux 通过本地 Docker 隔离契约，不调用 Provider | 已闭环 |
| RC-P3-02 | P3 | Task 详情字段与完整文件呈现 | B-13/Task 详情合同 | monitor snapshot → task detail UI | `plowwhip/monitor.py`、`plowwhip/ui.py`、`plowwhip/app.py`、测试 | role/model/generation/Checker/Evidence/handoff/session segment 可见可验 hash | 已闭环 |
| RC-P3-03 | P3 | Docker 8750 本地运行链验收 | B-03/A-07/E18 | image → single Cronner lease → 8750 → Host Bridge boundary | Docker/Compose 配置、只读验收 Artifact | 本地重建并重启 8750；health、进程、端口、卷、lease、20 行日志；不调用模型 | 已闭环 |
| LIVE-P0-01 | P0 | Cursor Planner 常不返回可解析结构化 JSON，主线卡在 needs_decision | P-01/P-07/A-16 | Planner HostJob(cursor_cli) → parse_planner_result | `plowwhip/planner.py`、`plowwhip/lifecycle.py`、`plowwhip/provider.py` | 真实 cursor_cli Planner 产出可解析 JSON；失败不得把主人决定当 TaskSpec | 已闭环 |
| LIVE-P0-02 | P0 | Planner 失败后 `provide_decision` 把主人决定正文装成执行 TaskSpec/objective 并跳过正式 Plan（含 RETRY_PLANNER 文案） | P-11/L-01/R-01/C-04 | needs_decision(plan) → provide_decision → execute_dispatch | `plowwhip/lifecycle.py`、`plowwhip/intake.py` | 决定只恢复/重试 Planner 或授权 Plan；禁止决定文本成为 provider_task instruction；Planner 失败应 cancel+重提 Goal | 已闭环 |
| LIVE-P1-01 | P1 | formal result manifest 为空仍进入 verify/needs_decision 循环；错误代决会反复 execute | V3:A-31/R-01 | execute finalize → verification manifest | `plowwhip/execution.py`、`plowwhip/verification.py`、`continue_policy.py` | 缺 manifest 时 fail-closed；禁同构继续；选项须 cancel/缩 Goal/换策略 | 已实现待回归 |
| LIVE-P1-02 | P1 | Host Bridge poll unavailable 时 Task 停在 execute_wait；GET /health 对 Bridge 返回 501 | V3:A-34 | HostJob poll → reconcile | `plowwhip/host_bridge.py`、`plowwhip/execution.py` | Bridge 探活/轮询合同明确；不可用时有界重试并呈现可读阻塞事实 | 已实现待回归 |
| LIVE-P2-01 | P2 | 语义查询取消长时间停在 stopping；取消路径与 lease 抖动 | V3:A-35/B-21 | cancel → HostJob stop | `plowwhip/lifecycle.py`、`plowwhip/execution.py` | 取消在 stop_grace 内收敛到 cancelled，不长期占 active Task | 已实现待回归 |
| LIVE-P0-03 | P0 | 项目管家对话里的「主人决定…」owner message 被当成正式指令，再次创建 planner_intake Goal | P-01/B-11/C-01 | POST /api/messages(owner) → _create_task | `plowwhip/lifecycle.py`、`plowwhip/butler.py`、`plowwhip/intake.py` | 决策/取消答复不得创建业务 Task；仅 action 或结构化 decision 推进 | 已闭环 |
| LIVE-P0-04 | P0 | Checker HostJob 已在 Bridge 成功，但 Cronner 因 cumulative token 回落崩溃，表现为 HostJob unavailable | A-05/A-10 | check_poll → record_model_call | `plowwhip/provider.py`、`plowwhip/lifecycle.py` | cumulative 回落钳制为 0 增量；「继续」可强制 reconcile 再 poll | 已闭环 |
| LIVE-DS-01 | P0 | Host Bridge 曾拒绝 `json-worker` 只读 Planner（DeepSeek 无法开跑） | B-02/A-05 | host_bridge read allowlist → Planner HostJob | `plowwhip/host_bridge.py` | deepseek/json-worker 只读 Planner 可 start | 已实现待回归 |
| LIVE-DS-02 | P1 | Host Bridge PATH 默认无 `simple-worker`，DeepSeek 探测/执行不可用 | B-02 | LaunchAgent PATH → simple-worker | 运行环境/`bridge` LaunchAgent | `simple-worker --probe` 经 Bridge 成功 | 受外部环境限制 |
| LIVE-DS-03 | P0 | `provider_models.deepseek≠default` 时 Bridge 向 `simple-worker` 传 `--model`，CLI 直接失败 | D-28/B-02 | selected_model → `_execution_argv` | `plowwhip/host_bridge.py`、`simple-worker` | 非 default 模型可派发或明确拒绝并递补 | 已实现待回归 |
| LIVE-DS-04 | P0 | DeepSeek/Kimi 被同化到同一 `json-worker` 合同（CLI/stdout/session），跨厂商「任意步骤递补」合同层易失效 | D-28/A-05/M-03 | PROVIDERS adapter=json-worker → fallback | `plowwhip/provider.py`、`host_bridge.py`、`provider_policy.py` | 递补按 executable 能力矩阵；stdout/模型传参分厂商 | 已实现待回归 |
| LIVE-DS-05 | P0 | Planner **verification（结构化解析）失败不触发** `_fallback_provider_generation`，仅进程失败才递补 | P-07/A-16 | plan_poll parse ValueError → needs_decision | `plowwhip/lifecycle.py` | 解析失败有界递补或 session 结果回灌后再判 | 已实现待回归 |
| LIVE-DS-06 | P0 | `simple-worker` 将 `PLOWWHIP_PLANNER_RESULT` 写在 session jsonl，Bridge stdout 仅有 progress/completed；`provider_agent_text` 不识别 `type=message` | P-07/A-05 | HostJob stdout → parse_planner_result | `host_bridge.py`、`provider.py`、`result_ingest.py` | DeepSeek Planner 成功完成且 stdout/回灌可被 parse | 已实现待回归 |
| LIVE-DS-07 | P1 | DeepSeek 已产出近合法 Plan，但 `upgrade_path=["simple→medium"]`（应为 `["simple","medium"]`）且 coverage 用文件路径而非 `TASK_KEY` 安全 id，导致 parse/normalize 失败 | P-09/P-11 | Planner JSON → normalize | `planner.py`（别名）/Prompt 约束 | 真实 DeepSeek Plan 可被 normalize；别名兼容箭头路径 | 已实现待回归 |
| LIVE-DS-08 | P0 | DeepSeek fullstack 反复 `internal_tool_no_progress`（limit=6），只读多文件后未 `write_file` 即失败；声明 Artifact 缺失；「继续」同构重试烧 Token | A-05/T-02 | simple-worker execute → snapshot | `host_bridge` context/`simple-worker` | 有界多文件审查能写出 Artifact 或提高/区分 progress 语义 | 已实现待回归 |
| LIVE-DS-09 | P0 | DeepSeek audit_delivery 在 `exploration`+limit=96 下仍 `bounded tool loop exceeded 48 turns`，读文件/compact 耗尽回合未写报告 Artifact | V3:A-29/MECH-05 | execute HostJob max_turns | `progress_policy.py`（audit_report max_turns=96）、`simple-worker` | audit_report 交付回合预算与 Cursor 可互换完成；缺干净 DeepSeek E2E | 已实现待回归 |
| LIVE-DS-10 | P1 | `simple-worker` `search_text`/`grep` 缺 `query` 键时 `KeyError` 直接进程失败（非合同失败） | B-02/MECH-06 | tool_result search_text | v2 `simple_worker.py` | 缺参返回结构化 error，不崩进程 | 已实现待回归 |
| LIVE-DS-13 | P0 | 代决文案 `继续：…` 不在 `CONTINUE_DECISIONS` 精确集合中，被 `provide_decision` 当成新 TaskSpec 重写并重跑 execute（rev+1），Checker 重试路径被绕过 | C-04/LIVE-P0-02 | provide_decision 分支 | `lifecycle.py` `_is_continue_decision` | 前缀 `继续`/`RETRY_PLANNER` 识别为 continue；verify 空 manifest 可 continue 重试 execute | 已实现待回归 |
| LIVE-DS-12 | P0 | DeepSeek Checker 把验收当成重做 Worker 审计：通读 plowwhip/*.py、反复 compact，未产出 `PLOWWHIP_CHECKER_RESULT`；stderr=`DEEPSEEK_API_KEY: TimeoutError`，exit 78，表现为 Checker exhausted | A-16/A-18/LIVE-DS-11 | check HostJob prompt / progress | `verification.py` `_checker_prompt` | **验收标准=报告已生成且有内容（含章节）**；Prompt 不粘贴 Worker 审查指令；max_turns≤16；禁止重审源码 | 已实现待回归 |
| LIVE-DS-11 | P0 | DeepSeek Planner 把 `PLOWWHIP_PLANNER_RESULT` **write_file 到隔离工作区**（如 `PLOWWHIP_PLANNER_RESULT.md`），stdout 仅有 “written to temp file” 散文；控制面判 `missing_structured_result`。UI 在 `phase=plan` 的任何 needs_decision 都提示「需要明确授权（15 分钟）」造成误导 | P-07/A-05/B-13 | HostJob finish harvest / parse / UI help | `host_bridge.py`、`planner.py`、`ui.py` | 工作区落盘结果在 rmtree 前回灌 stdout；中文 acceptance id / `provider_key` 可 coerce；仅在真正 awaiting authorization 时显示授权按钮文案 | 已实现待回归 |
| LIVE-DS-14 | P0 | `independent Checker rejected the planner result`（phase=`plan`）时，`provide_decision`+「继续」被通用分支拒绝为 `continue does not rewrite TaskSpec; retry Planner/Checker instead`，需二次「继续」才 `planner_retry`；烧代决预算 | C-04/LIVE-DS-13 | planner_intake continue 分支误排除 Checker rejected | `lifecycle.py` | phase=plan + Checker rejected → 一次「继续」即 `planner_retry_requested`；exhaustion 仍走 checker_retry | 已实现待回归 |
| LIVE-DS-15 | P0 | DeepSeek Plan `inputs` 常为文件路径字符串列表（如 `["plowwhip/a.py",…]`），normalize 抛 `unsupported input kind`；同签名恢复触 MECH-07 硬顶 | P-07/MECH-03 | `_normalize_task_inputs` | `planner.py` | 描述性 path list/map 强制 coerce 为 `owner_instruction`(+deps) | 已实现待回归 |
| LIVE-DS-18 | P0 | Checker 已输出 `PLOWWHIP_CHECKER_RESULT` PASS，但 json-worker exit≠0 → 控制面判 Checker exhausted，烧 MECH-07 | A-16/LIVE-DS-12 | apply_checker_step returncode 门闩 | `verification.py` | 有结构化 Checker 结果时以 verdict 为准，不唯 exit code | 已实现待回归 |
| LIVE-DS-20 | P0 | 同 spec_revision 重跑 execute 时 `register_indexed_artifact` 撞 UNIQUE，Cronner 崩事务回滚，HostJob 假 running，表象为 Host Bridge poll unavailable 空转 45 分钟 | LIVE-P1-01/A-05 | execute finalize → artifacts index | `artifact_contract.py` `register_indexed_artifact` | 同 path@revision 幂等 upsert；finalize 不再崩 | 已实现待回归 |
| LIVE-DS-21 | P0 | 审计报告四章已齐全，LLM Checker 仍 `did not prove every acceptance`，烧满 MECH-07 | **与 V3:A-16a 冲突** | check finalize | `verification.py` | **禁止**「章节齐全⇒覆盖语义拒」；合同 Checker 仅补充；假拒应修语义 Checker/Prompt/合同，不得标题短路 | 已实现待回归 |
| LIVE-DS-22 | P0 | 失败标准错误：工具空转计数杀仍在回复的模型；Task 无体量超时；超时直接停；同构重试烧 Token | V3:T-11～T-16/A-27～A-30 | HostJob budget / Cronner / butler | `timeout_policy.py`、`soft_timeout.py`、`io_phase.py`、`simple_worker`、`host_bridge`、`lifecycle`、`execution` | I/O 显式态；soft≤3×2；hard idle 不翻倍；T-14 增量门闩已实现 | 已实现待回归 |
| LIVE-DS-23 | P0 | 缺声明 Artifact + `internal_tool_no_progress`（或同类）时，「继续」/同构 `decision_retry` 仍重开同等大 HostJob 烧 Token | V3:A-31 | provide_decision continue / provider_recovery | `continue_policy.py`、`lifecycle.py`、测试 | 禁同构继续；ND 结构化选项已落地（耗尽后无 isomorphic continue） | 已实现待回归 |
| LIVE-DS-25 | P0 | Planner `returncode!=0` 先于结构化解析即失败，Cursor/json-worker 已写 `PLOWWHIP_PLANNER_RESULT` 仍判失败 | V3:R-19 | plan_poll | `lifecycle.py` | completed + 可解析结果优先；已升格入 V3 | 已闭环 |
| LIVE-DS-26 | P0 | Execute/Bridge 仅 `returncode==0` 才 succeeded/apply，已写 Artifact 也可能不落地 | V3:R-19 | host_bridge `_finish` / execution finalize | `host_bridge.py`、`execution.py` | staged payload/落地优先；已升格入 V3 | 已闭环 |
| LIVE-DS-27 | P0 | Checker 强制 acceptance 集合全等 + 非空 `recheck_command` → 漏字段假拒 | V3:A-16'/LIVE-DS-24 | `_parse_checker_verdict` | `verification.py`、测试 | 覆盖 frozen id 即可；recheck 可选 | 已闭环 |
| LIVE-DS-28 | P1 | Prompt 仍写「整行开头 marker」，与 LIVE-DS-24 解析兼容不一致 | LIVE-DS-24 | checker/planner-checker prompt | `verification.py`、`lifecycle.py` | Prompt 允许 mid-line marker；recheck 标注 optional | 已闭环 |
| LIVE-DS-24 | P0 | Cursor Checker 已输出 `PLOWWHIP_CHECKER_RESULT PASS`，但解析要求整行以 marker 开头且从后往前命中转义脏行 → `missing checker evidence` 伪拒，MECH-07 | V3:R-18/MECH-02 | `_parse_checker_verdict` / result_ingest | `result_ingest.py`、`verification.py`、测试 | 嵌套/转义/脏行可解析；缺更多真实 stdout 回归 | 已实现待回归 |

## LIVE-DS-22 超时续命现场记录

（soft 超时自动追加：原预算→翻倍后预算；结论含「Task 超时阈值判断有误」。）

- 2026-07-25 闭环证据：`docs/runtime-audits/LIVE_DS_22_REGRESSION_EVIDENCE_20260725.md`（pytest `tests/test_timeout_policy.py` 7 passed；Task `ec7f134d…` Done）

## LIVE-DS-23 缺 Artifact 同构重试跟跑

- 2026-07-25 22:24:19 LIVE-DS-23 Cursor 验收：Task `c7e70e030c4f499ea7425bd322066ffe` outcome=done; 证据 `docs/runtime-audits/LIVE_DS_23_CURSOR_REGRESSION_EVIDENCE_20260725.md`


（Cursor CLI / 8750 实现禁同构「继续」；卡点追加于此。）

| MECH-08 | P0 | 审计类 Goal 仍逼任意 Provider 吐完美线缆 Plan JSON；字段漂移反复 needs_decision / MECH-07；且易误写成厂商绑定 | V3:P-01'/R1 | parse_planner_result salvage | `planner.py` `build_audit_delivery_plan`、`lifecycle.py` | 控制面模板合法但**必须合同 Checker**；禁跳过验收的 salvage | 已实现待回归 |
| LIVE-DS-16 | P0 | Planner Checker 在空 sandbox 中因「plowwhip/*.py 不存在」判 invented scope，拒绝合法 Goal 派生 Plan | V3:P-17/MECH-03 | `_prepare_planner_checker` prompt | `lifecycle.py` | 空 sandbox≠发明范围；按 Goal 文本验收 Artifact | 已实现待回归 |
| MECH-01 | P0 | **角色感知 ProgressPolicy**：进度不能等同于 workspace 写；Planner/审计/Checker 用 exploration，Implement 用 mutation | V3:A-27～A-30 | `start_provider_job` / Bridge `context_policy` | `progress_policy.py`、`provider.py`、`host_bridge.py`、测试 | 已升格入 V3；定向测试有；DeepSeek 现场互换未证 | 已实现待回归 |
| MECH-02 | P0 | **统一结果摄入**：stdout ∪ session/message ∪ tool 嵌套 → Evidence；Planner/Checker 不依赖 Cursor 形 stdout | V3:R-18 | HostJob finish → parse | `result_ingest.py`、`planner.py`、`verification.py`、`host_bridge.py` | 已升格入 V3；单测有 | 已实现待回归 |
| MECH-03 | P0 | **Planner 沙箱认识论**：空 sandbox ≠ 资料不足；失败返回可递补错误码 | V3:P-17 | parse/normalize → needs_decision | `planner.py`、`planner_errors.py`、`lifecycle.py` | 已升格入 V3 | 已实现待回归 |
| MECH-04 | P0 | **验证失败有界递补**：解析/合同失败触发 fallback 或结构化重试，禁止无差别「继续」 | V3:A-33 | plan_poll/verify → fallback | `lifecycle.py`、`verification.py` | 已升格入 V3；ND 点选尚未完全替换自由文本继续 | 已实现待回归 |
| MECH-05 | P1 | **审计 TaskSpec 合同**：审查交付 Evidence/Report，中间允许只读；不套实现类写盘验收 | V3:A-29 | TaskSpec → verification | `planner.py`、`execution.py`、`progress_policy.py` | 已升格入 V3 | 已实现待回归 |
| MECH-06 | P0 | **json-worker 能力矩阵**：模型标志/结果形状/progress 按 executable 区分，禁止 DeepSeek/Kimi 合同同质化 | V3:A-11' | PROVIDERS → argv/fallback | `provider.py`、`host_bridge.py` | 已升格入 V3；派发前过滤仍需对照 | 已实现待回归 |
| MECH-07 | P0 | **同问题恢复硬顶 5**：`provider_retry/fallback`、`decision_retry`、`planner/checker_retry` 累计 ≥5 后 fail-closed；禁止再「继续」；暴露卡点 | V3:A-32 | fallback / provide_decision | `recovery_policy.py`、`execution.py`、`lifecycle.py`、`intake.py` | failure-signature 分桶已实现；缺多厂商现场烧满桶回归 | 已实现待回归 |
| V3-ND | P0 | NeedsDecision 必须结构化选项点选（理由/依据/利弊），禁止自由文本改写 TaskSpec | V3:ND-01～05/B-22 | needs_decision → UI/actions | `lifecycle.py`、`ui.py`、`intake.py` | decision_options 生成与点选 action；通用决定框禁用 | 已实现待回归 |
| V3-AB | P0 | 废止强制 Plan A/B；大型单一 Plan + DAG | V3:P-07废止/P-09' | planner normalize / UI | `planner.py`、`lifecycle.py`、测试 | 删除 A/B 强制与 0.95 打断；更新回归 | 已实现待回归 |
| V3-SCRIPT | P0 | 简单/本地脚本：库检索→复用→生成缺件→拼接→Local Runner→门禁入库 | V3:P-04'/D-32～D-34 | planner facts → local_script → promote | `script_library.py`、`local_script_worker.py`、`planner.py`、`verification.py`、测试 | 有检索证据；禁模型 shell；auto promote 同门禁；完整 DAG 可复现 | 已实现待回归 |
| V3-PROVIDER | P1 | 默认 cursor→deepseek；项目 Provider 管理面；派发前过滤 | V3:B-04'/B-27/A-11' | settings → dispatch/fallback | `store.py`、`intake.py`、`provider_policy.py`、`ui.py` | DEFAULT 无 Codex-first；disabled/unavailable 跳过；管理页只冻新 Session | 已实现待回归 |
| V3-NAV | P1 | Token/Monitor 独立主导航 | V3:B-25/B-26 | `ui.py` HTML nav | `plowwhip/ui.py`、测试 | 顶栏含 Token/Monitor；设置页不再吞并 | 已实现待回归 |

### 机制修复原则（2026-07-26 对齐 V3）

- **优先级（与 V3 G-01～G-03 一致）**：**正确证据完成 → 无人值守可达 → 极致省 Token**。互换是无人值守的手段，不能压过证据硬度。
- **同失败签名 ≤5 次重试**：满 5 次 = 机制卡点，停止递补/同构「继续」，修机制后再开跑。
- 不以盲目抬阈值/「继续」烧 Token 当主修复；缺口记入 **能力矩阵差**，禁止合同同质化。
- 目标态：DeepSeek/Kimi 与 Cursor **可互换但非同一 stdout/写盘假设**。
- **LIVE-DS-11**：解析失败时 UI 不得显示授权文案——已升格为 V3 **B-23**。

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

- **V3 纠偏（2026-07-26）**：原「四类产品页、Token/Monitor 塞设置」与 V3:B-25/B-26 **冲突**，状态改回**待复核**。验收改为六导航（全局/项目/Task/Token/Monitor/设置），Token/Monitor 独立页。
- 安全完整文件入口：Task snapshot 为 Artifact、Evidence、Handoff、Session segment 生成 `Task ID + opaque file_id` URL；后端从 Task 权威索引重新解析受控路径，并在返回完整 bytes 前复核 SHA-256 和 revision。浏览器不能提交本地 path；伪造 file_id 返回 404。
- 详情字段：Task 页同时展示 role/Checker、Provider/model、Worker、TaskSession、物理 Session、generation、Token、HostJob、Artifact/Evidence/Handoff 的 path/SHA/revision 和完整 Session segment 入口。模型名来自最新 ModelCall 事实，不再对非本地 Session 显示空占位。
- 观察标签：API 字段改为 `observation_tail` 并返回不可作为 Artifact/Evidence/完成依据的声明；UI 同样把默认 20 行标为有界观察。
- 反例验证：完整文件 200、伪造 ID 404、响应 SHA header、相对路径边界和观察标签仍有效；**导航页数断言须按 V3 重写**（旧「仅四页」测试若仍存在则与基线冲突）。

### RC-P2-02

- 设置真源：Global/Project/Task+Role 新增 `provider_models`，与 `provider_order` 一起进入 SQLite 并在 TaskSession 创建时冻结实际值和来源。dispatch 从冻结 settings 解析 model，经 ProviderStep 传给 Bridge；HostJob 记录 requested/actual model。
- 环境边界：Bridge 私有 env 和安全子进程 env 均拒绝 `DEEPSEEK_MODEL`/`KIMI_MODEL`，只保留部署/地址、opaque credential 和运行目录项。Codex、Cursor、JSON Worker 的显式模型选择统一转成受控 CLI 参数；`default` 也是 SQLite 中可追溯的显式选择。
- 反例验证：非法模型名拒绝；项目模型设置冻结来源；私有 env 中模型键拒绝；宿主环境中的模型键不会进入子进程；Bridge HostJob 保留 requested model。

### RC-P2-03

- **V3 纠偏（2026-07-26）**：原「普通 PASS 永不自动登记」与主人锁定的脚本模块管线 B1 / V3:D-34 **冲突**（代码已有 `enqueue_auto_promote_script`）。状态改回**待复核**。新合同：Checker PASS + 脚本门禁后允许控制面 auto enqueue `promote_script`；与主人显式 promote 走**同一** AST/Evidence/SHA 校验；已复用模块不重复晋升；非脚本 PASS 仍不得晋升。
- 双重门禁：intake 与 lifecycle 都复核 Task 终态、TaskSpec `script_contract`、Artifact ID/path/SHA/revision/source、当前完整 Checker Evidence。Python 文件还必须是 UTF-8 单文件、含独立 public callable、`main` CLI 和 `SystemExit(main())`；TaskSpec 必须声明包含 0/非 0 的 exit codes、stdout/stderr 合同及三项不同 acceptance。
- additive lineage：schema v10 只增 `library_items.source_task_id/source_artifact_id/source_revision`，脚本正文复制为版本化 library 文件，SQLite 仍只保存索引、revision、SHA 与来源。
- 反例验证：无合同、Evidence 不完整、缺 CLI guard 均拒绝；通过后脚本 bytes/SHA 与源 Artifact 一致且 lineage 完整；须补 auto-enqueue 与「已复用不重复晋升」定向测试后才能再标闭环。

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


### RC-P3-03

- 镜像：`plowwhip-web:v2-baseline`（`--network none` 构建自 `blue@5c0dcda` 工作树）。
- 容器：`plowwhip-web-v1-8750` 监听 `127.0.0.1:8750`，`healthy`，user `65534:65534`，卷 `plowwhip-web-v1-8750-data`，保留 `.cronner.lock`。
- 证据文件：`docs/BASELINE_V2_DOCKER_8750_RUNTIME_EVIDENCE.zh-CN.md`。
- 状态：本机 Docker 单实例验收闭环。

### LIVE-2026-07-25 Cursor-only Planner→Done

现场目标：`check-code` 强制 `provider_order=cursor_cli`，提交只读审查指令，验证 Planner→Done。

已复现并记入台账：

1. **LIVE-P0-01**：Cursor Planner 多次 `Planner output is invalid: Planner did not return a structured result` / candidates exhausted。
2. **LIVE-P0-02**：对 plan 阶段 `provide_decision` 后，objective/spec 变为「主人决定…」并进入 `execute_dispatch`，跳过正式 Plan 安装；`formal result manifest is empty`。
3. **LIVE-P0-03**：`POST /api/messages` 发送「主人决定：取消…」也会新建 `planner_intake` Task，形成取消风暴。
4. **LIVE-P1-02**：执行中出现 `Host Bridge poll unavailable; idempotent reconcile scheduled`；Bridge HTTP GET `/health` 返回 501。

代决记录：`docs/runtime-audits/OWNER_DECISIONS_2026-07-25.md`
运行日志：`/tmp/plowwhip_owner_proxy_e2e.jsonl`

#### 2026-07-25 闭环证据（Cursor-only Planner→Done）

- Goal 标记：`E2E_CURSOR_ONLY_20260725_113901`
- Task：`bd0753e5fe4f4209bc019d93195dfa25` → `outcome=done` / `phase=done` / `spec_revision=2`
- Provider：`check-code.provider_order` 全角色 `cursor_cli` only
- 交付：`docs/runtime-audits/CURSOR_UNATTENDED_PLANNER_TO_DONE.md`（四章非空；主机约 16709 bytes；含路径/SHA256 登记）
- 主线 HostJob：Planner command ✓ → Planner Checker ✓ → authorize/select_plan → Execute ✓ → Independent Checker ✓
- 关键修复（本轮）：
  1. Cursor Planner 字段别名（inputs/checker role/`md` format 等）
  2. Bridge 完整 stdout 分页不盲信 `has_more`
  3. `provide_decision`/`继续` 不污染 TaskSpec；Checker 耗尽重试 Checker
  4. Plan 授权必须 `instruction=task_id`
  5. 「outcome is unknown」时「继续」强制 reconcile poll
  6. `record_model_call` 对 cumulative 回落钳制，避免 Cronner tick 崩溃
- 运行日志：`/tmp/plowwhip_owner_proxy_e2e.jsonl`
- 当前结论：**Cursor-only 主线已在 8750 跑通 Planner→Done（含独立 Checker PASS）。**

### LIVE-2026-07-25 DeepSeek-only Planner→Done（观察轮，本轮不修代码）

现场目标：`check-code` 全角色 `provider_order=["deepseek"]`，`provider_models.deepseek=default`（规避 LIVE-DS-03），验证能否自动跑通审查交付。

已复现并记入上表：LIVE-DS-01～LIVE-DS-08。

关键证据（Task `2e34684223a04b9b9a738e70d2a8c09c` / Goal `E2E_DEEPSEEK_ONLY_20260725_135422`）：

1. HostJob `7c6904be…`：`--model deepseek-v4-flash` → `simple-worker: unrecognized arguments`（LIVE-DS-03）。
2. HostJob `39aaf080…`：worker.completed，session 含 `PLOWWHIP_PLANNER_RESULT`，但 lifecycle `planner_rejected`：stdout 无可解析结构化结果；根因含 LIVE-DS-06 + LIVE-DS-07（`upgrade_path`/`coverage` 合同）。
3. 验证失败路径走 `needs_decision`，**未**触发 Provider 递补（LIVE-DS-05）；且本 Goal 冻结唯一 deepseek，候选本就耗尽。
4. 本轮约定：**只记录不修复**。为省 Token 且继续「用 DeepSeek 完成审查」，主人代决对已产出意图执行 `provide_plan`（coverage id 改为安全 token，settings 锁定 deepseek），再监视 fullstack/Checker；**不**循环 `继续` 重跑 Planner。
5. fullstack HostJob 多次 `worker.completed`/`failure_class=internal_tool_no_progress`（例 session `352d39a5…`），交付物未出现；代决 `cancel` 停止烧 Token（LIVE-DS-08）。
6. **本轮结论：DeepSeek-only 未能自动跑通 Planner→Done**；阻塞为 LIVE-DS-03/05/06/07/08（下轮再修）。

代决记录：`docs/runtime-audits/OWNER_DECISIONS_2026-07-25.md`
问题旁路：`/tmp/plowwhip_deepseek_live_issues.jsonl`
运行日志：`/tmp/plowwhip_owner_proxy_deepseek.jsonl`（若存在）

### MECH-2026-07-25 机制修复开干

- 台账新增 MECH-01～MECH-06；LIVE-DS-08 → 进行中（由 MECH-01/05 吸收）。
- **MECH-01 已实现待回归**：
  - 新增 `plowwhip/progress_policy.py`：`exploration`（read/planner）vs `mutation`（write）。
  - `provider.start_provider_job` / `host_bridge._context_policy` 合并角色进度；Bridge 子进程注入 `PLOWWHIP_PROGRESS_MODE`。
  - 外部 `simple-worker`（v2）按 `PLOWWHIP_PROGRESS_MODE=exploration` 将唯一只读探查计为进度。
  - 验证：`tests.test_progress_policy` + 相关 Bridge 合同测试 OK。
- **MECH-02 已实现待回归**：
  - 新增 `plowwhip/result_ingest.py`；`parse_planner_result` / Checker 解析走 `ingest_provider_result`。
  - 裸标记行与 JSONL message/tool 均并入 `agent_text`。
  - 验证：`tests.test_result_ingest` OK。
- **MECH-04 已实现待回归**：
  - Planner 结构化解析 `ValueError` 调用 `_fallback_provider_generation(..., retry_same_provider=True)`，有候选则 `provider_fallback`，耗尽才 `needs_decision`。
  - 验证：`test_planner_parse_failure_falls_back_instead_of_immediate_decision` OK。
- **MECH-03 已实现待回归**：
  - `plowwhip/planner_errors.py`：`PlannerContractError` + 稳定 `error_code`（含 `planner.sandbox_false_insufficient`）。
  - lifecycle `planner_rejected` 事件携带 `error_code`；`fallback_eligible` 控制是否递补。
- **MECH-05 已实现待回归**：
  - 审查/报告 Artifact 打 `audit_delivery`；execution 注入 `progress_delivery=audit_report` → exploration（允许先多文件读再写报告）。
  - 纯只读 analysis 仍走 `evidence` + `workspace_change_required=false`（既有单测保持）。
- **MECH-06 已实现待回归**：
  - `PROVIDER_CAPABILITIES`：deepseek/kimi `model_transport=env` / `result_shape=session_markers`。
  - 非 default 模型对 deepseek/kimi 在 `start_provider_job` 明确 400 拒绝以便递补（LIVE-DS-03）。
- 验证：`tests.test_mech_planner_audit` + 相关 MECH/vertical 用例 OK。
- 下一动作：重建/重启 8750 + Host Bridge（含 v2 simple-worker）后跑 DeepSeek-only E2E。

### LIVE-2026-07-25 DeepSeek MECH E2E（主轨，未 Done）

- Marker：`E2E_DEEPSEEK_MECH_20260725_153014`
- Task：`dbd9b24f144945e58a4f9ec329c03a6e` → **cancelled**（执行失败耗尽后有界取消；报告未落盘）
- **已打通（互换进展）**：
  1. Planner HostJob：`progress_mode=exploration` / limit=96
  2. 结构化解析失败 → typed `error_code` + 同 Provider retry（MECH-03/04）
  3. Planner Checker PASS → 授权 → fullstack `execute` 启动
  4. Execute 携带 `progress_delivery=audit_report` + exploration（MECH-05 生效）
- **能力矩阵缺口（阻挡 Done）**：
  1. **LIVE-DS-09**：多次 execute `failure_class=internal_tool_no_progress` / `bounded tool loop exceeded 48 turns`；读+compact 烧尽回合，未 `write_file` 报告（单次输入 Token 可达 70万级）
  2. **LIVE-DS-10**：`simple-worker` grep/`search_text` 缺 `query` → `KeyError` 进程失败
  3. Bridge poll unavailable 仍偶发（LIVE-P1-02），需 wake/reconcile
- **补丁（本轮继续）**：`audit_report.max_turns=96`；simple-worker search 缺参容错；不切 Codex 交差。

- **E2E watch5 (2026-07-25 16:28:04)**：DeepSeek E2E `E2E_DEEPSEEK_MECH_20260725_160201` 硬顶/卡点停跑：same problem recovered 5 times (cap=5); blocking mechanism gap must be fixed before continue; prior=formal result manifest is empty

- **E2E (2026-07-25 16:28:38)**：旧 Task `d7a29ad315ed4eef987c8273e0f44035` 因 LIVE-DS-13 TaskSpec 被毁掉 + MECH-07 硬顶取消；重提 `E2E_DEEPSEEK_MECH_20260725_162832`。Checker 合同已改为**只验报告生成与内容**。

- **E2E watch5 (2026-07-25 16:41:49)**：DeepSeek E2E `E2E_DEEPSEEK_MECH_20260725_162832` 硬顶/卡点停跑：same problem recovered 5 times (cap=5); blocking mechanism gap must be fixed before continue; prior=Planner output is invalid: task audit_and_report has unsuppo

- **E2E watch5 (2026-07-25 16:44:42)**：DeepSeek E2E `E2E_DEEPSEEK_MECH_20260725_162832` 终态失败 outcome=cancelled；wait=

- **E2E (2026-07-25 16:46:48)**：旧 `E2E_DEEPSEEK_MECH_20…` 前一轮 `f14898…` 触 MECH-07（prior=unsupported input kind）。已修 LIVE-DS-15/16 并重建 8750；重提 `E2E_DEEPSEEK_MECH_20260725_164645` Task `55d5e77abe3c40e6a75c0e72f3c276cf`。

- **E2E watch5 (2026-07-25 16:49:43)**：DeepSeek E2E `E2E_DEEPSEEK_MECH_20260725_164645` 终态失败 outcome=cancelled；wait=

- **E2E watch5 (2026-07-25 17:10:22)**：DeepSeek E2E `E2E_AUDIT_TMPL_20260725_170602` 硬顶/卡点停跑：same problem recovered 5 times (cap=5); blocking mechanism gap must be fixed before continue; prior=independent Checker rejected the planner result

- **E2E MECH-08 (2026-07-25 17:10:26)**：`E2E_AUDIT_TMPL_20260725_170602` 失败 wait=

- **E2E LIVE-DS-19 (2026-07-25 19:48:58)**：`E2E_AUDIT_TMPL_20260725_194454` **Done** ok=True bytes=5554

- **MECH-07 cap**：`E2E_FIX_PLAN_20260725_200353` prior=same problem recovered 5 times (cap=5); blocking mechanism gap must be fixed before continue; prior=declared workspace Artifact is missing from the complete post-execution snapshot
- LIVE-DS-22 / soft-timeout: Task `068087f25568410a88e44a2ed2b8d616` project `deadline` class=hard attempt=1/3 budget 1800s（不再翻倍）; io_phase=idle; 结论：Task 超时阈值判断有误（hard idle — 无近期模型/工具 I/O）。
- LIVE-DS-22 / soft-timeout: Task `5624b046f71e403ca70a78a6c484903a` project `deadline-late` class=hard attempt=1/3 budget 1800s（不再翻倍）; io_phase=idle; 结论：Task 超时阈值判断有误（hard idle — 无近期模型/工具 I/O）。

## LIVE-DS-22 回归证据跟跑记录
- 2026-07-25 21:05:25 E2E_REG_EVIDENCE_20260725_210045 task=`ec7f134d…` outcome=done；交付 `docs/runtime-audits/LIVE_DS_22_REGRESSION_EVIDENCE_20260725.md`；中途 Planner exhausted 代决「继续」一次后恢复。
- 2026-07-25 21:23:03 E2E_LIVE_DS23_CURSOR_20260725_211903 task=`3cd936954a4e4b68b6f119d90cc45833` decision=cancel api=202 wait=same problem recovered 5 times (cap=5); blocking mechanism gap must be fixed before continue; prior=Planner output is invalid: task implemen
- 2026-07-25 21:23:07 E2E_LIVE_DS23_CURSOR_20260725_211903 TERMINAL outcome=cancelled task=`3cd936954a4e4b68b6f119d90cc45833`
- 2026-07-25 21:23:38 LIVE-DS-23 首跑失败：项目 settings 禁用 cursor_cli 且 planner=deepseek；Task `3cd93695…` MECH-07 cancelled。已切 Cursor 对照轨后重提。
- 2026-07-25 21:46:16 LIVE-DS-23 cancel task=`d35a601600eb4dd7a44135d3dc0660c7` wait=same problem recovered 5 times (cap=5); blocking mechanism gap must be fixed before continue; prior=independent Checker
- 2026-07-25 21:46:20 LIVE-DS-23 TERMINAL outcome=cancelled task=`d35a601600eb4dd7a44135d3dc0660c7`
