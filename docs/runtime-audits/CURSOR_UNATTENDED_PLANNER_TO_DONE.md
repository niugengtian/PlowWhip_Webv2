# CURSOR_UNATTENDED_PLANNER_TO_DONE

> Task：`bd0753e5fe4f4209bc019d93195dfa25` · spec revision 2  
> 模式：只读审查（唯一写入=本文件）；Provider 约束=`settings.fullstack.provider_order=["cursor_cli"]` 唯一，禁止任何递补  
> 审查日：2026-07-25  
> 执行方：cursor_cli only（无 Codex / DeepSeek / Kimi 或其它 Provider 递补痕迹）

---

## 所审路径与 SHA256 登记

### Goal 枚举的 9 个 plowwhip 模块

| 路径 | SHA256 |
|---|---|
| `plowwhip/planner.py` | `ecc3f59a8edd4dfdb8409ac42c63dab5a6848fdc0c9f526a5c098bf3466224b3` |
| `plowwhip/lifecycle.py` | `60715a33c4e109721b8b1fd9389d85e2224b8c381e96e14dfe5a04e235a0eec0` |
| `plowwhip/lifecycle_state.py` | `0f49643ee7627d77464ec1b332dea5ca0c22e4ab89c76ccbd5160e1df9ba2d3a` |
| `plowwhip/execution.py` | `2ccb897c465371d58b81a836f5cec8c94705c688adacc0f82a7340f9bd036b80` |
| `plowwhip/verification.py` | `7ce6d47dae6116aa75700a59d5338385d2d26d0694e208b64fb8626db2b292ca` |
| `plowwhip/artifact_contract.py` | `d657f45fbd239d7db75eb952b03843e4bcbe873bbad933db7db98de157349fe3` |
| `plowwhip/cronner.py` | `71668656dbf89e030956299624d16dbae2d85b986498b1c9f166771526e12ec1` |
| `plowwhip/continuity.py` | `7194fc38d4c956b2d3af7356cff800e6a2ca2d5431853006332c7935115d6ac9` |
| `plowwhip/host_bridge.py` | `c135ce867c915d73b47e72a15672822bd5409d026d8eb3f27e1de4f466feb5c9` |

### 对应 tests（按 import / 主线覆盖映射；仓库无独立 `test_planner.py` 等单文件）

| 路径 | 主要对应模块 | SHA256 |
|---|---|---|
| `tests/test_lifecycle_ownership.py` | `lifecycle_state` / 生命周期写边界 | `93940c9dc00afb8980a27b0782243a53b5b5cbb473220a58ecc60b82f373cb2a` |
| `tests/test_artifact_contract.py` | `artifact_contract` / `continuity` 依赖消费 | `f5bea2a195ae5f70c92af1ff16f00e692b6b1974fe842147b98c5b5349adfd77` |
| `tests/test_host_bridge.py` | `host_bridge`（含 planner 私有只读 workspace） | `d26dbfbd7b4e6091398710644c525ed0903c9fb95ddb1fb6fb72cb451f3e9018` |
| `tests/test_vertical_slice.py` | `planner`/`lifecycle`/`execution`/`verification`/`cronner`/`continuity` 端到端主线 | `f57f9a590a2d499641b28f6e14098e0974af50ecaf0f6bd41b67284c244535c1` |
| `tests/test_review_fixes.py` | `execution`/`verification`/`lifecycle_state`/`host_bridge` | `03aa280356c58f9a98da4cef726c47214f5162749a3b872a27d3209d50c0db7f` |
| `tests/test_live_p0_owner_decision.py` | `planner`/`cronner`/`lifecycle` 实况 P0 | `119052a9c6cb7f324d6e3b5253108a1bed9a6adbb754ead8cd27167369dbd169` |

### 只读对照的 V2 基线相关 docs

| 路径 | SHA256 |
|---|---|
| `docs/BASELINE_V2_INDEPENDENT_AUDIT_2026-07-25.zh-CN.md` | `7470818cd3b21120600ff157bde2c70222b63fdfa097c75e8b86f4b3eb213802` |
| `docs/BASELINE_V2_POST_MERGE_REAUDIT_2026-07-25.zh-CN.md` | `35ba99eb8bd26691de8be6714ec2dc0fce3aaa2a6e3bd27fc6cad53b2d4ab736` |
| `docs/BASELINE_V2_REMEDIATION_LEDGER.zh-CN.md` | `365e86e0a9cfdec61d3a38a9a7cd8aa39b624404636889e2f61baca5d4fc86ad` |
| `docs/BASELINE_V2_REAUDIT_EVIDENCE.zh-CN.md` | `f4acfc7b96f509fa213ce4e3e86545bb132355b2a64a324ee610b794e0979385` |
| `docs/BASELINE_V2_CONFORMANCE_AUDIT.zh-CN.md` | `9b553d741728dc1c5e843148ac28e76efc4a461ac6d74a05d060e77038c72cd8` |
| `docs/BASELINE_V2_DOCKER_8750_RUNTIME_EVIDENCE.zh-CN.md` | `34784394b9cbb6e40304a40e01da12559a7cdbf7608708336b708299818e472e` |

---

## (1) 总判

**Planner→Done 功能主线在当前工作区已贯通且可静态核验**：正式消息经 `_create_task` 一律建成 `kind=planner_intake` → 模型 Planner HostJob → `parse_planner_result` / `normalize_plan` → 独立 Planner Checker → `_install_plan` → Execute（`perform_provider_step`）→ Verify / 独立 Checker（`perform_checker_step`）→ `finalize_task_terminal`；Cronner 以 lease 驱动 `advance_project`，Artifact 以 path/SHA256/bytes/revision/scope/source 为真源（`register_artifact` / `verified_artifact` fail-closed）。

**相对 V2 基线仍不能宣称完全通过。** 独立审计与合入后重审认定的生命周期合同冲突在本轮源码抽检中仍成立：

1. **第二推进器**：`cronner._advance_due_project` 在 `advance_project` 返回后若 `checkpoint_project` 失败，调用 `record_checkpoint_failure`，后者自行 `lifecycle_write_scope("advance_project")` + `write_task_fields`→`needs_decision`（绕过“唯一由 advance_project 推进”的字面合同）。
2. **写所有权未闭合**：`execution.py` 21×、`verification.py` 12× 直接调用 `write_task_fields(`；另有多处 `finalize_task_terminal`。虽要求处于 `lifecycle_write_scope("advance_project")` 内，但状态决策仍在 Worker/Checker 路径完成，并非“只交事实由 lifecycle 归约”。
3. **台账过声称**：`BASELINE_V2_REMEDIATION_LEDGER` 将 RC-P0-03 / L-01 / L-03 标为「已闭环」，与独立审计及本轮调用链**不一致**；全仓无 `set_authorizer`。应以调用链为准。

另：通用递补实现 `_fallback_provider_generation` 按冻结 `provider_order` 取下一候选，并支持 `retry_same_provider` 同 Provider 换 generation。本 Goal 以 `["cursor_cli"]` 锁定时可阻断跨 Provider 递补，但机制仍在主线、多 Provider 配置与同 Provider retry 仍是无人值守风险面。本审计未改 `plowwhip/`、`tests/`、`Dockerfile`，第 4 章仅为建议未实施。

---

## (2) 主线证据

### 2.1 真实主线（代码路径）

```text
Cronner.tick
  → acquire lease → advance_project（打开 lifecycle_write_scope）
     → 未处理消息 → _create_task(kind=planner_intake, phase=plan)
     → phase=plan → _prepare_planner_step → perform_planner_step（Host Bridge, access=read, workspace_kind=planner）
     → 结构化 parse_planner_result → 独立 Planner Checker
     → PASS → _install_plan（占位 Task 转 phase=execute；DAG 后续 Task 入队）
     → phase=execute → execute_task / pending_provider_step → perform_provider_step
     → phase=verify → verify_task / CheckerStep → perform_checker_step
     → PASS → finalize_task_terminal(outcome=done) + capability 撤销事件
  → checkpoint_project（Warm/Cold）
  → （冲突点）checkpoint 失败则 record_checkpoint_failure 再写 Task
```

本轮静态核对：`plowwhip/*.py` 九模块 `ast.parse` 均 OK；`write_task_fields(` 计数 execution=21 / verification=12 / lifecycle=4；`finalize_task_terminal(` 计数 execution=2 / verification=2 / lifecycle=3。

### 2.2 分模块只读审查结论

| 模块 | 主线职责 | 关键证据 | 审查结论 |
|---|---|---|---|
| `planner.py` | 语义 sizing 权威；结构化 Plan/Task 合同硬校验；`instruction_facts` 不含 size | `instruction_facts`；`PLANNER_RESULT_PREFIX`；`parse_planner_result`/`normalize_plan`；`provider_order` 校验；`audit_then_repair` 门禁 | **主线可用**；Cursor 输出需有界 coerce（tests 已覆盖常见形态），失败须 fail-closed |
| `lifecycle.py` | 单动作推进；统一建 `planner_intake`；安装 Plan；编排 prepare/apply | `advance_project`；`_create_task` 仅 `planner_intake`；`_prepare_planner_step`；`_install_plan`；Planner 失败路径调用 `_fallback_provider_generation`；`record_checkpoint_failure` 第二写入口 | **功能贯通**；相对 V2 L-01/C-04 **合同未闭合** |
| `lifecycle_state.py` | Task 生命周期字段写门闩；终态前收敛；预算事实归约 | `lifecycle_write_scope`（仅允许 `advance_project`/`schema_migration`）；`write_task_fields` 要求 origin=`advance_project`；`finalize_task_terminal`；`apply_model_budget_fact` | **门闩有效**；无 SQLite authorizer，裸 SQL 可绕过 helper（ownership 测试只拦约定路径） |
| `execution.py` | Worker/Probe HostJob；正式输出 Artifact；Provider 失败/递补 generation | `perform_provider_step`；`_finalize_provider_job`；`_fallback_provider_generation`（`order[index+1:]` + 可选同 Provider retry） | **主线可用**；大量直接写 Task 状态 → V2 L-03/C-02 冲突仍在 |
| `verification.py` | 确定性验收与独立 Checker；PASS/CHANGES_REQUIRED/递补 | `verify_task`；`perform_checker_step`；`_enqueue_checker_fallback`；消费 `verified_artifact` | **主线可用**；Checker 路径同样直接 `write_task_fields`/`finalize_task_terminal` |
| `artifact_contract.py` | Artifact 登记与消费 fail-closed | `register_artifact`/`verified_artifact`；scope=`task_data`\|`workspace`；bytes+SHA256 复核 | **符合 R-01～R-08 意图**；篡改 path/hash/revision/source 拒绝 |
| `cronner.py` | 进程内唯一调度；due 项目每 tick 至多一动作 | `acquire_scheduler_lock`；`tick`→`_advance_due_project`→`advance_project`+`checkpoint_project`；失败分支 `record_checkpoint_failure` | **调度主线正确**；checkpoint 失败分支构成**第二推进器** |
| `continuity.py` | Warm handoff / Cold segment；递补前校验最新 handoff | `checkpoint_project`；`checkpoint_task_session`→`verified_artifact`；`compile_hot_context` 有界、不持久化 Hot | **连续性合同成立**；checkpoint 失败由 cronner 升格为决策态 |
| `host_bridge.py` | 宿主进程边界；capability；deadline 事实；append-only stream | `_validated_capability`；`deadline_reached_at`（超时标事实，非伪造成自动 kill 合同）；`stream_capture`；`usage_kind` | **Bridge 边界成立**；timeout reconcile 顺序与 V2 T-06/T-07 一致 |

### 2.3 对应 tests 覆盖点（抽样）

- **写边界**：`test_lifecycle_ownership.py` — 禁止 lifecycle 外 Goal/Task SQL；scope 外 `write_task_fields` 抛错且 DB 不变。
- **Artifact 完整消费**：`test_artifact_contract.py` — 多依赖 + >1MiB；篡改 path/hash/revision/source fail-closed。
- **Planner/Checker/递补/continuity**：`test_vertical_slice.py` — 如 `test_every_formal_instruction_is_semantically_planned_before_execution`、`test_message_to_verified_done`、`test_planner_checker_rejection_blocks_plan_installation`、`test_planner_checker_provider_fallback_reuses_checked_artifact`、`test_hot_warm_cold_continuity_is_bounded_and_append_only`、`test_checkpoint_overflow_converges_to_explicit_needs_decision`、`test_owner_wake_is_queued_but_cronner_remains_the_only_driver`、`test_terminal_provider_failure_falls_back_with_new_generation`。
- **Bridge**：`test_host_bridge.py` — planner 私有只读 bridge workspace 等。
- **实况 P0 / Cursor 解析宽容**：`test_live_p0_owner_decision.py` — `parse_planner_result` 对 upgrade_evidence 字符串与 descriptive inputs 的 coerce；`provide_decision` 不得改写 TaskSpec；主人决定消息不得再建业务 Task。
- **review 修复回归**：`test_review_fixes.py` — execution/verification/lifecycle_state/host_bridge 交叉回归。

### 2.4 本执行 Provider 边界（审计元数据）

本 Task 合同锁定 `provider_key=cursor_cli` 且 `fullstack.provider_order=["cursor_cli"]`。审查过程仅只读上述路径并写入本 MD；未调用 Codex/DeepSeek/Kimi，未实施递补配置变更，无其它 Provider 递补痕迹。

---

## (3) 相对 V2 基线的 P0 缺口

对照权威输入：`docs/BASELINE_V2_INDEPENDENT_AUDIT_2026-07-25.zh-CN.md` 与 `docs/BASELINE_V2_POST_MERGE_REAUDIT_2026-07-25.zh-CN.md`；不以 `BASELINE_V2_REAUDIT_EVIDENCE` / 台账自述「冲突=0 / RC-P0-03 已闭环」为最终证据。本轮对 Goal 枚举 9 模块源码复检后，**仍存在 P0 缺口**如下。

| ID | 缺口 | 本轮代码证据 | V2 条款/台账对照 |
|---|---|---|---|
| **GAP-P0-01** | 第二生命周期推进器 | `cronner.py`：`advance_project` 后 `checkpoint_project` 失败 → `record_checkpoint_failure`；`lifecycle.py:155+` 自行开 `lifecycle_write_scope("advance_project")` + `write_task_fields`→`needs_decision` | 独立审计 L-01/D-09/C-04「与基线冲突」；合入后重审仍成立；台账 RC-P0-03「已闭环」**过声称** |
| **GAP-P0-02** | 非 lifecycle 模块决定并写入 Task 状态 | `execution.py` 21×、`verification.py` 12× `write_task_fields(`；另有多处 `finalize_task_terminal`。SQL 字符串虽收口到 lifecycle*，但状态决策仍在 Worker/Checker 路径内完成 | 独立审计 L-03/C-02；ownership 测试只拦裸 SQL / scope 外调用，**不证明**“只交事实” |
| **GAP-P0-03** | 台账 LIVE 主线仍开放（无人值守阻断） | 台账 `LIVE-P0-01` Cursor Planner 常无结构化 JSON；`LIVE-P0-02` `provide_decision` 污染 TaskSpec；`LIVE-P0-03` 「主人决定…」再创 Goal | 台账状态=进行中；对应 tests 已部分门禁，但实况稳定性仍依赖 Cursor 输出与决策卫生 |
| **GAP-P0-04** | Provider 递补机制仍在主线（配置可触发） | `_fallback_provider_generation`（`execution.py`）按 `provider_order` 切片下一 Provider 并换 generation；Planner/Checker 失败路径同样调用；另有同 Provider `retry_same_provider` | 基线 B-04/B-05「静态顺序、无竞价」已实现；**无人值守 Cursor-only 场景需把“无递补”升为硬合同**，当前仅靠 settings 长度 |
| **（范围外但独立审计仍列）** | 第三写入口 `POST /api/semantic-search`；Secret 启发式非密码学边界 | 不在 Goal 枚举 9 模块内；独立审计 B-15 冲突 / D-30 部分实现仍记 | 验收前最小闭合清单项；本审计不实施 |

**结论**：Planner→Done **功能主线可用**；相对 V2 **P0 合同缺口仍在**——尤其是唯一推进器字面闭合、写归约所有权、以及 LIVE Planner/决策污染导致的无人值守中断。**并非“无 P0 缺口”。**

---

## (4) 无人值守 / 优质代码 / 省 Token 建议（只建议，不实施）

### 4.1 无人值守

1. **删除第二推进器**：`checkpoint_project` 失败只记 `task_events`/`messages` 事实，下一 tick 由 `advance_project` 归约为 `needs_decision`；禁止 `record_checkpoint_failure` 再开写 scope。
2. **execution/verification 只返回 facts**：所有 `write_task_fields`/`finalize_task_terminal` 上收到 `lifecycle` reducer；模块输出结构化 outcome fact，避免“租界内各自改状态”。
3. **Cursor-only 硬门**：当 Task/Goal 声明禁止递补或 `provider_order` 长度为 1 时，短路 `_fallback_provider_generation`（含 Checker），失败直接结构化 `needs_decision`，禁止同 Provider 隐式换 generation 烧配额。
4. **LIVE-P0 决策卫生**：禁止把「主人决定…」正文建成 `planner_intake` Goal；Planner 失败只允许 cancel+resubmit 或结构化 `RETRY_PLANNER` action，禁止 `provide_decision` 改写 `spec_json`。
5. **Planner 输出协议降脆**：保留强制前缀行 JSON；对 Cursor 常见形态继续做有界 coerce（已有 upgrade_evidence/inputs/checker 别名），但 coerce 失败必须 fail-closed 并带可机读 `blocking_reason`，避免空转 tick。

### 4.2 优质代码

1. 为 L-01 增加反例测试：断言 `cronner` 路径在 checkpoint 失败后**不得**在 `advance_project` 返回后再次写 Task 生命周期列。
2. 静态计数门禁：CI 扫描 `write_task_fields(` 调用点仅允许 `lifecycle.py`（或单一 reducer 文件）。
3. 统一 Planner/Worker/Checker 的 HostJob purpose 与 phase 命名表，减少 `plan_call`/`check_call`/`execute_wait` 分支漂移。
4. 台账状态与独立审计冲突时，以调用链+反例测试为准，禁止“已闭环”覆盖未删入口。

### 4.3 省 Token

1. Planner Prompt 已很长：将稳定合同（acceptance shape、authorization 枚举、script_contract）外置为**版本化短 schema 引用**，Prompt 只留语义 sizing 与本 Goal 差分，降低每票固定 input。
2. Hot context：`compile_hot_context` 已有字节上限；递补/重试时优先复用已哈希 Artifact 引用（Planner Checker fallback 已示范），避免重跑 Planner。
3. 观察尾（20 行）永不进正式 Prompt；Checker Prompt 禁止附带 Worker 自由聊天（现有合同应保持）。
4. 无进度自动重试设置默认偏低；Cursor-only 项目关闭跨 Provider fallback，减少失败链路的二次/三次整票调用。
5. 语义归纳继续「先精确后模型」；精确命中时 `model_queued=false`，避免无谓语义 HostJob。

---

## 审计边界声明

- 已覆盖 Goal 枚举 9 个 `plowwhip` 模块及上表对应 tests；V2 docs 仅只读对照。
- 唯一写入：`docs/runtime-audits/CURSOR_UNATTENDED_PLANNER_TO_DONE.md`。
- 未修改：`plowwhip/`、`tests/`、`Dockerfile`；未 git commit/push；未 Docker/部署/外部发送。
- 第 4 章建议均未实施为代码或配置变更。
- Provider：本执行唯一 `cursor_cli`，无 Codex/DeepSeek/Kimi 或其它递补实施痕迹。
