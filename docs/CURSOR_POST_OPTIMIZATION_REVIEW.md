# Cursor 第 3 代独立审查报告（Codex P1 优化后）

> 历史审查记录：本文审查的 Token reservation/预算门已由 `0021_remove_token_budget.sql` 废止。当前只保留消费计量，外部网络与环境故障策略仍独立生效。

审查日期：2026-07-17  
审查基线：分支 `codex/sprint-9-execution-continuity`，未提交工作树（含 `0014_token_reservations.sql` 及关联运行时改动）  
对照文档：`报告.md`（优化前独立审查）、`docs/CODEX_OPTIMIZATION_RESULT.md`（实现方自证）  
审查方式：只读代码与 diff 审阅；沿用上一轮已执行的定向测试结论（`179 passed`，含 `test_host_job_continuity.py`、`test_context_budget_rotation.py`、`test_database.py` 全通过）。本文件为唯一写入产物。

## 1. 执行结论

Codex P1 优化**实质性闭环**了原 `报告.md` 中三项最高优先级生产缺陷：

1. Host Provider `token_budget=0` 仍可派发 → **已闭环**
2. `max_parallel_workers` 只限制单次 Tick 新增、不扣已在途 Host Job → **已闭环**
3. Context 截断优先丢失 Boundaries / Completion rule → **已闭环**

Artifact 证据链有显著加强（SHA-256 / 字节数 / mtime 进入 evidence hash；stable fingerprint 排除 mtime），但**来源性（provenance）仍未强制**，不能宣称“完成证据必为本 run 新建”。

原 `报告.md` 多项 P1 **仍未闭环**：Permission 非执行门、Convention 精炼不进统一预算/预留、strict 非独立审查。此外，本轮审查新确认 **Host 完成路径存在 Token 双重入账风险**，以及 **usage stream 解析器对嵌套 JSON 取 max 可能暴露会话累计值（含 5,551,318 类异常显示）**——二者均应按 P1 处理。

**总体判断**：作为单机、可信操作者的本地 MVP，预算预留、在途并发硬门、Context 安全截断已达到可验收水平；作为“Token 总账准确、权限强制执行、强来源完成证据”的无人值守系统，**尚不满足可靠投产条件**。

---

## 2. 验证证据摘要

| 验证项 | 结果 | 证据 |
|---|---|---|
| 工作目录 / 分支 | 通过 | `/Users/niugengtian/work/plow-whip-web-v2`，`codex/sprint-9-execution-continuity` |
| 迁移 `0014` | 存在 | `backend/plow_whip_web/store/migrations/0014_token_reservations.sql` |
| 后端全量测试 | 通过 | 上一轮执行：`.venv/bin/python -m pytest` → **179 passed in 4.44s** |
| Host 零预算拒绝 | 通过 | `tests/test_host_job_continuity.py:160-179` |
| 全局预留防超卖 | 通过 | `tests/test_host_job_continuity.py:182-232` |
| claim 后崩溃释放预留 | 通过 | `tests/test_host_job_continuity.py:235-263` |
| 跨 Tick 并发硬门 | 通过 | `tests/test_host_job_continuity.py:266-297` |
| 外部中断结算预留 | 通过 | `tests/test_host_job_continuity.py:518-566` |
| Context 安全截断 | 通过 | `tests/test_context_budget_rotation.py:82-110` |
| Artifact evidence hash | 通过 | `tests/test_host_job_continuity.py:443-459` |
| 真实 CLI / 5,551,318 现场复现 | **未执行** | 仓库内无该字面量；见下文专项分析 |

---

## 3. 专项验收：Codex 声称的五项优化

### 3.1 Host Provider Token 预算预留

| 子项 | 结论 | 文件 / 行号 | 证据 |
|---|---|---|---|
| 迁移与表结构 | **已闭环** | `0014_token_reservations.sql:1-16` | `run_id` PK、`active/settled/released` 状态、`reserved_tokens > 0` CHECK |
| claim 同事务预留 | **已闭环** | `task_repository.py:188-286` | `BEGIN IMMEDIATE` 内读 settings、`_in_flight_count`、任务/全局日额度（usage + active reservation）、`INSERT token_reservations` |
| `token_budget=0` 拒绝 | **已闭环** | `budget.py:30-34`；`task_service.py:72-73` | `host_reservation()` 在 remaining≤0 抛 `BudgetExceededError`；测试 `test_host_task_zero_budget_is_rejected_before_claim_and_dispatch` |
| 结算 / 释放 | **已闭环** | `budget.py:62-80`；`task_service.py:216-228` | `record()` 将 active→settled；无上报量则 `release()`；中断路径 `add_to_task=True` |
| stale recovery 释放 | **已闭环** | `recovery.py:51-57` | claim 后无 Host Job 时 `UPDATE token_reservations SET status='released'`；测试 `test_recovery_releases_reservation_if_claim_crashes_before_host_job` |
| `/api/usage` reserved | **后端已闭环、前端未展示** | `budget.py:101-111`；`api/app.py:327-330` | API 返回 `reserved_tokens`；`web/src/api.ts:121-128`、`web/src/App.tsx:294-295` 的 `Usage` 类型与 UI **未包含**该字段 |
| Provider 侧硬截止 | **未闭环（设计边界）** | `docs/CODEX_OPTIMIZATION_RESULT.md:19` | 仅为账本预留门；CLI 超预留后只能事后入账，不能撤回费用 |

**原子性评估**：并发 claim 由 SQLite `BEGIN IMMEDIATE` 串行化；预留与 attempt/run 创建在同一事务——**满足原子预留**。  
**幂等性评估**：`token_usage` 使用 `INSERT OR IGNORE` + `run_id`（`budget.py:43-53`）；`finish` / `claim` 均有 idempotency_key 去重——**账本写入幂等基本成立**。  
**缺口**：Host **正常完成**路径见 §4 P1-7（`tokens_used` 可能双重累加）。

### 3.2 系统级最大在途并发

| 子项 | 结论 | 文件 / 行号 | 证据 |
|---|---|---|---|
| 在途计数口径 | **已闭环** | `task_repository.py:307-318` | `UNION`：`running`/`verifying`/`stopping` 任务 + 未消费 `host_jobs` |
| Scheduler 扣减 | **已闭环** | `scheduler.py:70-72` | `available_slots = max(0, max_parallel_workers - active)` |
| claim 二次硬门 | **已闭环** | `task_repository.py:212-213` | 事务内 `_in_flight_count >= max_parallel_workers` → `ResourceBusyError` |
| 跨 Tick / 手工 drive | **已闭环** | `tests/test_host_job_continuity.py:266-297` | 第二 Tick `selected==0`；手工 `drive` 抛 `parallel worker limit` |

原 `报告.md` P1-3（连续两 Tick 留下 2 个 Host Job）→ **已闭环**。

### 3.3 Artifact evidence hash 与 stable fingerprint

| 子项 | 结论 | 文件 / 行号 | 证据 |
|---|---|---|---|
| 文件元数据入 evidence | **已闭环** | `verification.py:37-57,87-96` | `sha256`、`bytes`、`modified_at_ns` 写入 checks 并进入 canonical evidence hash |
| 内容变化改变 hash | **已闭环** | `tests/test_host_job_continuity.py:443-459` | 修改 `result.txt` 后 `evidence_hash` 改变 |
| stable fingerprint | **已闭环** | `task_service.py:327-353`；`task_repository.py:377-379` | `_failure_fingerprint` 递归剔除 `modified_at_ns`；防循环用 fingerprint 而非 raw hash |
| 文件必须为本 run 产生 | **未闭环** | `docs/CODEX_OPTIMIZATION_RESULT.md:60-61` | 实现方自述：默认不强制 provenance；`file_exists` 仍可通过执行前既有文件 |

原 `报告.md` P1-5 → **部分闭环**（完整性加强，来源性未强制）。

### 3.4 Context 安全截断

| 子项 | 结论 | 文件 / 行号 | 证据 |
|---|---|---|---|
| 不可丢弃段 | **已闭环** | `context.py:61-64,128-131` | `Boundaries` 与 `Completion rule` 进入 `protected`，超限抛 `DomainError` |
| 优先级收缩 | **已闭环** | `context.py:49-60,143-151` | global(0) < continuation(1) < objective(3) < project(4) < task(5)；低优先级先截 |
| 最小保留配额 | **已闭环** | `context.py:52-55,148-150` | project floor 768、task floor 1024 字节 |
| 回归测试 | **已闭环** | `test_context_budget_rotation.py:82-110` | 保留 task/project 规则、Boundaries、Completion rule；丢弃 global |

原 `报告.md` P1-6 → **已闭环**。

### 3.5 Host Bridge 与任务状态回归

| 子项 | 结论 | 文件 / 行号 | 证据 |
|---|---|---|---|
| 固定 argv / 无 shell | **已闭环（保持）** | `host_bridge.py`；`test_host_job_continuity.py:493-515` | Cursor 打开使用固定 argv 列表 |
| 项目根 allowlist | **已闭环（保持）** | `verification.py:77-84`；`test_host_job_continuity.py:462-490` | 路径逃逸拒绝 |
| 外部中断恢复 | **已闭环** | `task_repository.py:562-618`；`task_service.py:199-206` | 不消耗 attempt；session 保留；continuation 注入 `context.py:39-47` |
| 运行中取消 | **已闭环** | `task_service.py:230-262`；`test_host_job_continuity.py:569-587` | `stopping` → Host 确认 → `cancelled` |
| reconcile 幂等 | **已闭环** | `test_host_job_continuity.py:337-353` | 重复 reconcile 不重复写 `token_usage` |
| `dispatch_outcome_unknown` | **未闭环** | 原 `报告.md` P2-5 | 本轮 diff 未改此分支 |

---

## 4. 按优先级排列的发现

### P0 / 严重

**未发现**必然导致数据全损、未认证远程代码执行或系统完全不可用的 P0 缺陷。

---

### P1 / 高风险

#### P1-1 Host 完成路径 `tokens_used` 双重累加 — **未闭环**

**证据**

- `task_repository.finish()` 在验证完成时直接把 execution token 加入任务累计：

```390:403:backend/plow_whip_web/store/task_repository.py
            token_delta = int(execution.get("input_tokens", 0)) + int(execution.get("output_tokens", 0))
            connection.execute(
                """
                UPDATE tasks SET status = ?, revision = ?, tokens_used = tokens_used + ?,
```

- Host Job `completed`  reconcile 先调 `_finish_execution()`（触发上述 `finish`），再调 `_settle_host_reservation()`：

```155:173:backend/plow_whip_web/runtime/task_service.py
                elif task.status in {TaskStatus.RUNNING, TaskStatus.VERIFYING}:
                    execution = self.provider_pool.bridge.result(snapshot)
                    ...
                    result = self._finish_execution(...)
                ...
                self._settle_host_reservation(task, job, snapshot)
```

- `_settle_host_reservation` 在 `add_to_task=True` 时再次累加 `tasks.tokens_used`：

```216:226:backend/plow_whip_web/runtime/task_service.py
        if int(execution["input_tokens"]) + int(execution["output_tokens"]) > 0:
            self.budget.record(
                task, execution, provider=task.provider, run_id=job["run_id"],
                add_to_task=True,
            )
```

```54:61:backend/plow_whip_web/runtime/budget.py
            if add_to_task and inserted.rowcount:
                connection.execute(
                    """
                    UPDATE tasks SET tokens_used = tokens_used + ?,
```

**影响**：Host 正常完成时，任务 UI / 预算门看到的 `tokens_used` 可能为实际 CLI 用量的 **2 倍**；后续 `host_reservation()` 基于错误累计可能过早拒绝重试。外部中断路径仅走 `_settle_host_reservation`（不经 `finish` token 累加），故测试 `test_interrupted_host_job_reuses_session_without_spending_attempt`（`tokens_used == 5`）不能覆盖此缺陷。

**对照**：`报告.md` 未单独列出；属于优化引入的结算路径组合回归。

---

#### P1-2 usage stream 解析取 max，可能暴露会话累计 Token（含 5,551,318 类异常）— **未闭环**

**证据**

- 流式与最终解析均对 JSON 树中所有 `input_tokens`/`output_tokens` 字段取 **最大值**，非求和、非按 turn 去重：

```314:318:backend/plow_whip_web/host_bridge.py
                    record["input_tokens"] = max(
                        int(record.get("input_tokens") or 0), int(parsed["input_tokens"])
                    )
                    record["output_tokens"] = max(
                        int(record.get("output_tokens") or 0), int(parsed["output_tokens"])
                    )
```

```658:670:backend/plow_whip_web/host_bridge.py
def _parse_stream(output: str) -> dict[str, object]:
    ...
        input_tokens = max(input_tokens, _find_int(event, {"input_tokens", "inputTokens"}))
        output_tokens = max(output_tokens, _find_int(event, {"output_tokens", "outputTokens"}))
```

- `_find_int` **递归遍历整棵 JSON**（`host_bridge.py:689-699`），会拾取嵌套对象中的 token 字段。

**5,551,318 Token 分析**

- 仓库内**无**字面量 `5551318` / `5,551,318`；该数字应来自真实 CLI JSON stream 某事件的**会话级或生命周期累计 usage**，而非本 run 增量。
- 若厂商事件为**累计快照**，`max()` 作为终值解析**理论上正确**（`test_provider_pool.py:146-150` 仅覆盖单事件 17+9 的简单场景）。
- 若流中同时存在：① 每 turn 增量 usage；② 嵌套的 session/thread 累计 usage（大数如 5,551,318），`max()` 会选中大数，导致：
  - Host Job snapshot / `token_usage` 入账异常偏高；
  - 与 P1-1 叠加时，UI 可能显示约 **2× 累计值**；
  - 预留结算后任务预算被快速耗尽，但**不能证明 CLI 真消费了该量**。
- 原 `报告.md` §9.2 已标注“取最大值是否正确取决于厂商语义，尚未用真实 CLI 验证”——**本轮仍成立**。

**结论**：Parser 策略在真实多事件流上**未验证**；存在把**会话累计 parser 值**当作**本次 run 实际用量**暴露入账的风险。应按 P1 跟踪，需用真实 Codex/Cursor stream 样本补充测试。

---

#### P1-3 Convention 精炼不受统一预算 / 预留约束 — **未闭环**（继承 `报告.md` P1-2）

**证据**

- `providers/pool.py:137-173`：`refine_convention` 直接 `bridge.execute`，不调用 `BudgetManager.ensure` / `host_reservation`，不写 `token_usage` / `token_reservations`，仅写 `convention_refinements`。
- `budget.py:83-113`：`summary()` 只汇总 `token_usage`，精炼消耗不出现在全局日预算与 `reserved_tokens`。

**影响**：UI“今日 Token”与全局预算可漏计明确标注“计 Token”的精炼调用。

---

#### P1-4 Permission 仍为决策记录，非执行门 — **未闭环**（继承 `报告.md` P1-4）

**证据**

- `permission_repository.py:44-57` 实现 `is_allowed`。
- 全后端生产路径 **零调用**（上一轮 Grep 仅命中 repository 定义）；任务 claim、Host Bridge、Generic Command 均不查询权限。

**影响**：`deny` 记录不能阻止任何动作。

---

### P2 / 中风险

#### P2-1 Generic Command 路径 `BudgetManager.ensure` 仍非原子 — **未闭环**（继承 `报告.md` P2-4）

**证据**：`budget.py:14-28` 用独立读连接查当日 `token_usage` 总量，与后续 claim/执行不在同一事务。Host 路径已改走 claim 内预留；generic-command 仍有并发超额窗口。

#### P2-2 strict “独立审查”仍非独立 — **未闭环**（继承 `报告.md` P2-1）

**证据**：`task_service.py:278-281` 再次实例化同类 `VerificationEngine()`；Host 路径 `task_service.py:159-161` 对同一 execution 调两次 `verify_host_task`。

#### P2-3 `no_progress_count` 仍等同 `same_failure_count` — **未闭环**（继承 `报告.md` P2-3）

**证据**：`task_repository.py:383`：`no_progress_count = 0 if passed else same_failure_count`。

#### P2-4 Artifact 无 provenance 策略 — **未闭环**（部分继承 `报告.md` P1-5）

**证据**：`verification.py:39-43` 仅检查文件存在；不验证 mtime 变化发生于本 run 之后。实现方在 `CODEX_OPTIMIZATION_RESULT.md:60-61` 明确暂不启用。

#### P2-5 前端未展示 `reserved_tokens` — **未闭环**

**证据**：后端 `budget.py:111` 已返回；`web/src/api.ts:121-128` 类型缺失；`web/src/App.tsx:294-295` Usage 视图无预留指标。运维无法从 UI 区分“已消费”与“已预留未结算”。

#### P2-6 `dispatch_outcome_unknown` 人工恢复缺口 — **未闭环**（继承 `报告.md` P2-5）

**证据**：本轮 diff 未修改 `host_bridge.py` 该分支；无安全人工解决 API。

#### P2-7 `FaultPolicy` 未接入生产控制流 — **未闭环**（继承 `报告.md` P2-2）

---

### P3 / 一般风险与工程债

1. **测试缺口**：无针对 Host 完成路径 `tokens_used` 非双倍断言；无多事件累计/增量混用 stream 的 parser 测试；无 `reserved_tokens` API/UI 契约测试。
2. **前端 SSE 仍未接入**（继承 `报告.md` P2-6）。
3. **非 loopback 前端无 Bearer**（继承 `报告.md` P2-7）。
4. **CI 未构建 Docker / 真实 HTTP E2E**（继承 `报告.md` P3-3）。
5. **Host Bridge 覆盖率仍偏低**（继承 `报告.md` P3-2）。

---

## 5. 原 `报告.md` P1 闭环对照表

| 原编号 | 主题 | 优化前 | 本轮结论 |
|---|---|---|---|
| P1-1 | Host Provider 绕过 Token 预算 | 未闭环 | **已闭环**（预留 + `token_budget=0` 拒绝） |
| P1-2 | Convention 精炼不进总账 | 未闭环 | **未闭环** |
| P1-3 | 最大并发非在途总量 | 未闭环 | **已闭环** |
| P1-4 | Permission 非执行门 | 未闭环 | **未闭环** |
| P1-5 | 证据不绑定 artifact 内容 | 未闭环 | **部分闭环**（hash 含内容/metadata；无 provenance） |
| P1-6 | Context 截断丢失安全尾段 | 未闭环 | **已闭环** |
| （新） | Host 完成双重 token 入账 | — | **未闭环** |
| （新） | usage parser 累计值风险 / 5,551,318 | 部分提及 §9.2 | **未闭环** |

---

## 6. 最终验收判断

| 验收维度 | 判断 |
|---|---|
| Host `token_budget=0` 派发前拒绝 | **通过** |
| 全局日额度并发预留防超卖 | **通过**（claim 事务内） |
| 中断 / stale recovery 预留释放 | **通过** |
| `max_parallel_workers` 在途硬门 | **通过** |
| Context 保留 Boundaries / Completion rule | **通过** |
| Artifact evidence 含内容 hash | **通过** |
| Artifact 必须为本 run 新建 | **不通过** |
| Token 总账准确（含任务累计） | **不通过**（P1-1、P1-2） |
| Token 预算硬停止（含精炼） | **不通过**（P1-3；Provider 侧无硬截止） |
| Permission 强制执行 | **不通过** |
| strict 独立审查 | **不通过** |

**综合评级**：Codex P1 优化**可信地修复**了原审查中最危险的三条调度/预算/上下文缺陷；但 **Token 显示与入账准确性**、**Permission**、**精炼预算**、**证据来源性** 仍阻止将系统宣称為“可无人值守且费用可控”的完整产品。建议优先修复 P1-1（双重入账）与 P1-2（stream 解析语义验证），再处理继承项 P1-3、P1-4。

---

*审查人：Cursor 第 3 代独立验证会话*  
*本报告仅基于代码、diff 与上一轮测试输出；未调用真实 Codex/Cursor/DeepSeek 服务。*

CURSOR_REVIEW_COMPLETE
