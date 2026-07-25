# PlowWhip Web V2 基线独立符合性审查报告

> 审计日期：2026-07-25  
> 模式：只读；未修改生产代码、数据库、Docker、任务或蓝绿环境  
> 结论：相对初始审计已大幅收敛，但**仍不能宣称完全通过 V2 基线验收**

## 1. 总判

当前工作区（`main@942f246` + 未提交 V2 修复）相对初始审计（36 项冲突）已修复绝大多数 P0 主线问题：正式指令统一进入模型 Planner、Artifact/Checker 合同、timeout reconcile 顺序、Cold append-only、Token cumulative 差分、四页 UI 等均有生产路径。

但仍存在可核验的基线冲突与未闭合项：

1. **生命周期唯一推进器未闭合**：`cronner` 在 `advance_project` 之后可调用 `record_checkpoint_failure`，自行打开写作用域并改 Task 状态。
2. **模块写入所有权未闭合**：`execution` / `verification` 仍大量直接调用 `write_task_fields` / `finalize_task_terminal`，不是只交事实由 `lifecycle` 归约。
3. **写入口未完全收敛**：除 `POST /api/messages` 与 `POST /api/actions` 外，存在 `POST /api/semantic-search`。
4. **Secret 合同是启发式拒收**，不是不可绕过的存储边界。
5. **A-07 生产单实例调度资格**缺少本审计复跑的 Docker/跨挂载证据。

编号条款共 **168** 项。本次机械统计：

| 结论 | 数量 |
|---|---:|
| 已实现 | 157 |
| 部分实现 | 5 |
| 仅文档 | 0 |
| 未实现 | 0 |
| 与基线冲突 | 6 |

与仓库内 `docs/BASELINE_V2_REAUDIT_EVIDENCE.zh-CN.md`（宣称 167 已实现 / 1 部分实现 / 0 冲突）**不一致**；差异集中在生命周期所有权与写入口的字面合同解释。本报告以基线条文与真实调用链为准，不以修复台账自述为准。

## 2. 审计范围与不可变边界

| 项 | 值 |
|---|---|
| 唯一基线 | `/Users/niugengtian/work/plow-whip-web-v2/docs/MINIMAL_REDESIGN_BASELINE_V2.zh-CN.md` |
| 基线 | 934 行、SHA-256 `4ae79ba906997640304a202ba0d453e2437e77e36468c29eb4a185ad970f6098` |
| 目标仓库 | `/Users/niugengtian/work/plow-whip-web_blue-1` |
| 起点 commit | `942f246d7b82dcfdcb74452c654646185a27067f` |
| 起点 tree | `4b403873193070c374703129a75b28bbd0e53c26` |
| 审计对象 | 上述 commit + 当前未提交工作区 |
| 工作区指纹 | `WORKTREE_BUNDLE = 9519acfcd03e4c850e846bbe7a3fdb99f21921f5af07197f9cece4ad304580f2`（`plowwhip/*.py` + `tests/test_*.py`） |
| 未修改 | 现有生产代码、测试、配置、数据库、Docker、任务、蓝绿、Git 历史 |
| 本审计唯一新增 | 本报告；旁路 Canvas 摘要（非代码） |
| 未做 | 付费 Provider、部署切流、无关全量系统压测 |

参考但不采信为证据：`docs/BASELINE_V2_CONFORMANCE_AUDIT.zh-CN.md`（修复前）、`docs/BASELINE_V2_REMEDIATION_LEDGER.zh-CN.md`、`docs/BASELINE_V2_REAUDIT_EVIDENCE.zh-CN.md`。

## 3. 方法与真实主线

### 3.1 方法

1. 完整阅读 V2 基线，机械提取 168 个唯一条款 ID。
2. 从 `POST /api/messages`、Butler、`submit_message`、Cronner `tick`、`advance_project` 追踪主线。
3. 核对 SQLite schema（15 张权威表）、Goal/Task SQL 写入者、`lifecycle_write_scope`、Artifact/Evidence、Host Bridge timeout、Secret、UI 写入口。
4. 运行无网络、本地可复现的 unittest；不把测试名称或 mock 当作生产合同证明。
5. 对旧审计/台账结论逐条复核，冲突处以代码为准。

### 3.2 当前真实主线

```text
POST /api/messages
→ butler.route_global_message / intake.submit_message（Secret 启发式拒收）
→ Cronner.tick：fcntl 文件锁 → project lease/fence
→ advance_project（打开 lifecycle_write_scope）
   → 正式消息一律 _create_task(kind=planner_intake)
   → phase=plan → Planner HostJob（模型）
   → parse_planner_result → 独立 Planner Checker
   → _install_plan / needs_decision
   → Execute HostJob → 确定性验证 / 独立 Checker
   → finalize / checkpoint Warm·Cold
→ （额外）checkpoint_project 失败时 record_checkpoint_failure 再写 Task  ← 冲突点
→ Monitor 只读 snapshot / 最后 20 行观察
```

### 3.3 相对初始审计的翻转（已修复的 P0）

| 根因 | 初始 | 本次 | 关键证据 |
|---|---|---|---|
| 正式指令跳过模型 Planner | 冲突 | 已实现 | `_create_task` 只建 `planner_intake`；无 `classify_instruction` |
| kind/正则决定最终 size | 冲突 | 已实现 | `instruction_facts` 不含 `size`；size 来自 PlannerResult |
| 多模块直接 `UPDATE tasks` SQL | 冲突 | SQL 已收口 | 裸 SQL 仅 `lifecycle.py` / `lifecycle_state.py` |
| 截断报告作正式输入 | 冲突 | 已实现 | `complete=True` 分块到 EOF；Artifact path/hash/revision |
| 模型产出无独立 Checker | 冲突 | 已实现 | Planner/Worker/minimal probe 均有独立 Checker Session |
| Bridge 先 SIGTERM 再 reconcile | 冲突 | 已实现 | Bridge 只标 `deadline_reached_at`；lifecycle `timeout_reconcile` 后再 stop |
| Cold 256KiB 覆写 | 冲突 | 已实现 | `stream_capture` append-only；segment 只增 |
| Token 一律标 single | 冲突 | 已实现 | adapter `usage_kind` + cumulative 相邻差 |
| 四页以外产品面 | 部分 | 已实现 | UI 顶栏仅四类视图 |

## 4. 逐条符合性矩阵

结论枚举严格为：`已实现 / 部分实现 / 仅文档 / 未实现 / 与基线冲突`。  
“验证证据”：`V-full` = 完整 unittest 90 OK（skipped=1）；`V-own` = lifecycle ownership 3 OK；`V-art` = artifact contract 2 OK；`静态` = 调用链/schema 核对。

### 4.1 Planner（P-01～P-15）与 C-01

| 条款 | 结论 | 代码证据 | 验证证据 | 差距 | 最小处理 |
|---|---|---|---|---|---|
| P-01 | 已实现 | `lifecycle.py` `_create_task`→`planner_intake`；`_prepare_planner_step` | V-full；`instruction_facts` 无 size | 无 | 保持 |
| P-02 | 已实现 | `planner.py:96-111` facts 非权威；size 仅自模型 JSON | 静态调用 `instruction_facts` | 启发式仍可作 input_facts | 保持；勿把 facts 升级为权威 |
| P-03 | 已实现 | 仅 simple/medium/large | 静态 | 无 | 保持 |
| P-04 | 已实现 | simple 限确定性/probe/git_publisher 合同 | 静态 | 语义质量仍靠 Planner+Checker | 保持 |
| P-05 | 已实现 | medium 恰好 1 fullstack Task + responsibility | 静态 | 多责任误判依赖 Checker | 保持硬校验 |
| P-06 | 已实现 | large 升级证据 + 原子责任硬门 | 静态 | 触发仍偏模型；非冲突 | 可选：结构化多模块事实 |
| P-07 | 已实现 | large ≥2 alternatives | 静态 | 无 | 保持 |
| P-08 | 已实现 | A/B 比较字段齐全 | 静态 | 无 | 保持 |
| P-09 | 已实现 | 客观占优 gate + confidence≥0.95 | 静态 | confidence 仍自报；客观占优是硬门槛 | 保持占优 gate |
| P-10 | 已实现 | DAG/环/唯一责任 | 静态 | 无 | 保持 |
| P-11 | 已实现 | task_contract 全字段 | V-art | 无 | 保持 |
| P-12 | 已实现 | upgrade_path + 证据版本化 | 静态 | 无 | 保持 |
| P-13 | 已实现 | Planner 出方案；lifecycle 安装 | 静态 | 无 | 保持 |
| P-14 | 已实现 | 结构化 blocking / NEEDS_DECISION；Prompt 禁直接问主人 | 静态 | Worker 自由文本无解析级封禁 | 可选 fail-closed 扫描 |
| P-15 | 已实现 | `_ensure_project_question` 跨项目单 waiting | V-full | 无 DB UNIQUE | 可选唯一约束 |
| C-01 | 已实现 | intake 不定 size | 静态 | 无 | 保持 |

### 4.2 生命周期所有权（L / D-09 / C-02 / C-04）

| 条款 | 结论 | 代码证据 | 验证证据 | 差距 | 最小处理 |
|---|---|---|---|---|---|
| L-01 | **与基线冲突** | `lifecycle.py:82-84` 与 `146-175`；`cronner.py:118-122` | V-own 未覆盖第二入口 | `record_checkpoint_failure` 不经 `advance_project` 推进 Task | **删除该入口**；改写 event/message，下一 tick 由 `advance_project` 归约 |
| L-02 | 已实现 | `_advance_project_transaction` 单动作 | 静态 | prepare/apply 多事务属 Host 释放设计 | 保持 |
| L-03 | **与基线冲突** | `execution.py` 21 处、`verification.py` 12 处 `write_task_fields(` | 静态计数 | Worker/Checker 路径直接写状态，非只交事实 | 迁入 lifecycle reducer；模块只返回 facts |
| L-04 | 已实现 | `monitor.py` 全 `connect_readonly`；`store.py` `mode=ro`+`query_only` | 静态 | 无 | 保持 |
| L-05 | 已实现 | `one_active_task_per_project`；needs_decision 阻塞新 intake | V-full schema | Goal 无 DB 唯一 active | 可选 Goal 约束 |
| L-06 | 已实现 | goals 无 status；monitor 从 Task 聚合 | 静态 | 无 | 保持 |
| D-09 | **与基线冲突** | 同 L-01 | 静态 | 后台循环绕过 `advance_project` | 同 L-01 |
| C-02 | **与基线冲突** | SQL 在 lifecycle*；**调用点**在 execution/verification | V-own 只查 SQL 字符串 | “只有 lifecycle 可写”字面未满足 | 写调用全部收口到 lifecycle |
| C-04 | **与基线冲突** | `record_checkpoint_failure` 第二推进器 | 静态 | 为 checkpoint 失败另开推进路径 | 删除第二推进器 |
| write scope | 协作门闩 | `lifecycle_state.py:34-58` ContextVar | V-own | 无 SQLite authorizer；裸 `UPDATE` 可绕过 helper | 优先删第二入口；必要时加连接包装 |

### 4.3 对象与状态（O / S）

| 条款 | 结论 | 代码证据 | 验证证据 | 差距 | 最小处理 |
|---|---|---|---|---|---|
| O-01～O-04 | 已实现 | `store.py` projects/goals/plans；sprint 字段分组 | schema 15 表 | 无 | 保持 |
| O-05 | 已实现 | Task + acceptance + 独立重试/验收 | V-art | 无 | 保持 |
| O-06～O-13 | 已实现 | workers/sessions/generations/host_jobs | 静态 | 无 | 保持 |
| O-14～O-17 | 已实现 | 生产代码无 Attempt/Episode/Run/Candidate/RoleInstance/… | 全库扫描 NONE | 无 | 保持 |
| S-01～S-06 | 已实现 | 四态 + outcome；phase/fault 内部；无禁态 | schema CHECK | 无 | 保持 |

### 4.4 自动机制与验证（A）

| 条款 | 结论 | 代码证据 | 验证证据 | 差距 | 最小处理 |
|---|---|---|---|---|---|
| A-01～A-06 | 已实现 | 应用内 Cronner；due/`next_action_*`；无补跑 | 静态 | 无 | 保持 |
| A-07 | **部分实现** | `fcntl` 锁 + project lease + candidate 禁用 Cronner | 代码有；Docker 本次未复跑 | 生产唯一资格跨挂载/镜像运行证据未闭合 | 获准后重建 8750 并记录 lease/进程证据 |
| A-08～A-14 | 已实现 | Evidence 完成；无 zero_progress；fallback 前 checkpoint；running 不切 Provider | 静态 | 无 | 保持 |
| A-15～A-20 | 已实现 | 确定性路径；模型→独立 Checker；无 Candidate | V-art + 静态 | Checker `report` 死参数未注入 Prompt（行为正确） | 可删死参数 |
| A-21～A-25 | 已实现 | Monitor 只读；默认 20 行；观察非 Evidence | UI/monitor 文案 | 无 | 保持 |

### 4.5 完整结果（R）

| 条款 | 结论 | 代码证据 | 验证证据 | 差距 | 最小处理 |
|---|---|---|---|---|---|
| R-01～R-08 | 已实现 | `artifact_contract` + execution manifest + 完整依赖消费 | V-art（>1MiB、篡改 fail-closed） | Hot 过压时 recent_artifacts 可截到 2（依赖链仍完整） | 勿把 Hot 截断当正式输入 |
| R-09～R-13 | 已实现 | purpose=check；probe 合同；exit0/自述不能单独 done | 静态 | evidence 型可落 `provider-report.md`（完整哈希，非 tail） | 保持与交付型区分 |
| R-14～R-16 | 已实现 | `audit_then_repair` Planner 门禁 + DAG done + 完整 Artifact | 静态 | 无 | 保持 |
| R-17 | 已实现 | 无第二结果状态机 | 扫描 | 无 | 保持 |

### 4.6 会话 / Token / 超时（M / T）

| 条款 | 结论 | 代码证据 | 验证证据 | 差距 | 最小处理 |
|---|---|---|---|---|---|
| M-01～M-09 | 已实现 | Hot/Warm/Cold；append-only；超限拒绝 | 静态 | 无 | 保持 |
| M-10～M-17 | 已实现 | cached⊆input；single/cumulative；跨 generation 累计 | 静态 | 无 | 保持 |
| T-01～T-05 | 已实现 | Global 600 可覆盖；large 强制 runtime；冻结来源 | 静态 | 无 | 保持 |
| T-06～T-10 | 已实现 | Bridge 报 deadline → reconcile/checkpoint → 冻结 grace stop | 静态 | 非固定 5s | 保持 |

### 4.7 存储 / Secret / 库（D）

| 条款 | 结论 | 代码证据 | 验证证据 | 差距 | 最小处理 |
|---|---|---|---|---|---|
| D-01～D-08 | 已实现 | SQLite WAL；15 权威表；无第二队列 | schema | 无 | 保持 |
| D-09 | **与基线冲突** | 见 §4.2 | — | — | — |
| D-10～D-19 | 已实现 | 目录组织；segment 只增；Task-ID 文件入口；library 文件真源 | V-full 文件 API | 无 | 保持 |
| D-20～D-26 | 已实现 | capability 硬校验；无自动升模板；promote_script 验收后入库 | 静态 | 无 | 保持 |
| D-27～D-29 | 已实现 | 业务模型在 settings；env 不透传模型选择 | 静态 | 无 | 保持 |
| D-30 | **部分实现** | `secret_policy.py` 已知形态拒收/redact；`intake` 调用 | 静态 | 非匹配明文仍可能入库；非密码学边界 | 扩大形态 + 终态引用失效已有则保持；勿宣称“绝不可能入库” |

### 4.8 部署 / Provider / Butler / 页面 / 权限（B）与 C-03

| 条款 | 结论 | 代码证据 | 验证证据 | 差距 | 最小处理 |
|---|---|---|---|---|---|
| B-01～B-03 | 已实现 | 单进程模块；Bridge 宿主边界；默认 8742 | 静态 | A-07 Docker 证据另计 | 保持 |
| B-04～B-07 | 已实现 | 静态 Provider 顺序；无竞价；探针显式 Task | 静态 | 无 | 保持 |
| B-08～B-12 | 已实现 | Butler 路由；精确先搜；语义入队 | 静态 | 语义走独立 POST，见 B-15 | 合并写入口 |
| B-13～B-14 | 已实现 | 观察警示；四顶栏；无 Attempt 页 | UI | 无 | 保持 |
| B-15 | **与基线冲突** | `app.py:168-184` 额外 `POST /api/semantic-search` | 静态 | 第三写入口 | **删除或并入** `/api/messages` 或有限 actions |
| B-16 | 已实现 | GET 只读；POST 需 idempotency | 静态 | 无 | 保持 |
| B-17～B-21 | 已实现 | 三档 capability；`.rm.*`；授权绑定；终态撤销 | 静态 | 无 | 保持 |
| C-03 | 已实现 | 模块函数调用；仅 Bridge HTTP | 静态 | 无 | 保持 |

## 5. P0～P3 差距合并（删除优先）

### P0：主线合同仍冲突

| ID | 合并根因 | 条款 | 最小处理 |
|---|---|---|---|
| GAP-P0-01 | 第二生命周期推进器 | L-01, D-09, C-04 | 删除 `record_checkpoint_failure` 的状态写入；只记事实，由下次 `advance_project` 归约 |
| GAP-P0-02 | 非 lifecycle 模块决定并写入 Task 状态 | L-03, C-02 | execution/verification 只返回 facts；所有 `write_task_fields`/`finalize_task_terminal` 调用迁入 `lifecycle.py` |
| GAP-P0-03 | 第三写入口 | B-15 | `semantic-search` 并入 messages/actions，删除独立 POST |

### P1：完成边界不完整

| ID | 合并根因 | 条款 | 最小处理 |
|---|---|---|---|
| GAP-P1-01 | Secret 启发式 | D-30 | 维持拒收；补常见形态；文档诚实表述“已知形态” |
| GAP-P1-02 | 生产单实例调度证据 | A-07 | Docker 8750 重建与 lease/进程验收（环境许可后） |

### P2：残余软依赖（不升格为冲突）

| ID | 说明 | 条款 |
|---|---|---|
| GAP-P2-01 | 多模块升级与“勿直接问主人”仍偏 Prompt/Checker | P-06, P-14 |
| GAP-P2-02 | 跨项目单问题无 DB UNIQUE | P-15 |
| GAP-P2-03 | Hot capsule 过压截断 recent_artifacts（正式依赖链不受影响） | R-06 周边 |
| GAP-P2-04 | write scope 无 SQLite authorizer | C-02 加固 |

### P3：可延后

- UI 视觉优化、非必要性能、动态 Provider 评分等基线已列延期项：本次无新增阻塞。

## 6. 验证证据

| 编号 | 命令/方式 | 结果 |
|---|---|---|
| V01 | 基线 `wc` + SHA-256 + 条款提取 | 934 行；hash 与 §2 一致；168 唯一条款 |
| V02 | `ast.parse` 全部 `plowwhip`/`tests` | 27 文件语法 OK |
| V03 | `:memory:` 执行 SCHEMA | 15 张应用表；四态 CHECK；`one_active_task_per_project` |
| V04 | `instruction_facts(...)` | 无 `size` 键；仅启发式事实 |
| V05 | `rg` Goal/Task 写 SQL | 仅 `lifecycle.py` / `lifecycle_state.py` |
| V06 | 禁止对象扫描 | Attempt/Episode/Candidate/`candidate_ready`/`zero_progress` 等 NONE |
| V07 | `unittest tests.test_lifecycle_ownership tests.test_artifact_contract` | 5 OK |
| V08 | `unittest discover -s tests`（非沙箱） | **Ran 90 tests … OK (skipped=1)** |
| V09 | 工作区指纹 | `WORKTREE_BUNDLE` 见 §2 |

未运行：付费 Provider、生产切流、本审计会话内 Docker 重建。

## 7. 与既有文档的分歧

| 文档 | 声称 | 本报告 |
|---|---|---|
| `BASELINE_V2_REAUDIT_EVIDENCE` | 167 已实现 / 1 部分 / 0 冲突 | 157 / 5 / 6；冲突见 GAP-P0-* |
| RC-P0-03“唯一 advance_project 打开 scope” | 已闭环 | **不实**：`record_checkpoint_failure` 也打开 |
| RC-P0-03“SQL authorizer” | 提及 | **未落地**（全仓无 `set_authorizer`） |
| B-15 已实现 | KEEP-B | **冲突**：存在 `/api/semantic-search` |
| D-30 已实现 | RC-P1-04 | **部分实现**：启发式边界 |

## 8. 验收建议

在宣称“V2 基线验收通过”前，至少闭合：

1. GAP-P0-01（第二推进器）  
2. GAP-P0-02（lifecycle 唯一写所有权）  
3. GAP-P0-03（写入口收敛）  
4. GAP-P1-02（A-07 Docker 证据）  

GAP-P1-01 可接受为“已知形态拒收 + 日志脱敏”的部分实现，但不得写进“Secret 绝不可能进入 SQLite”的绝对表述。

---

**审计声明**：本报告只读产出；未改代码。完整交互摘要见旁路 Canvas。
