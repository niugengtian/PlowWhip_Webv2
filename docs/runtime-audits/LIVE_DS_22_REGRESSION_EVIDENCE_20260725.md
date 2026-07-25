# LIVE-DS-22 回归证据报告

> 审计日期：2026-07-25 · 验证轨：deepseek（仅使用代码和测试证据）  
> 目标：墙钟预算 + I/O 显式态 + soft≤3×2 + hard idle 不翻倍  
> 审计方式：只读代码审查 + 自动化测试执行

---

## 总判

**LIVE-DS-22 回归通过。** 全部验收标准已有自动化测试覆盖或可静态追溯至确切代码位置。7 项自动化测试全部通过；台账留痕、Butler 消息、续命封顶均已实现。

| 验收项 | 状态 | 证据 |
|--------|------|------|
| I/O 显式态分类（soft vs hard idle） | ✅ Pass | `classify_timeout()` 按 `io_phase` + `last_io_at` 判 soft/hard；3 项单元测试验证 |
| soft 续命 ≤3×2 翻倍 | ✅ Pass | `MAX_SOFT_TIMEOUTS=3`；`next_soft_timeout_seconds()` 翻倍；`soft_timeout_allowed()` 封顶 |
| hard idle 不翻倍 | ✅ Pass | `try_soft_timeout_extension()` hard 分支返回 `hard_idle`，不调 `extend_provider_job_timeout` |
| Butler 消息上报 | ✅ Pass | `_record_butler_notice()` 写入 `messages` 表，idempotent |
| 台账留痕 | ✅ Pass | `_append_ledger()` → `append_timeout_ledger()` 追加至修复台账 |
| 体量感知超时门限 | ✅ Pass | `DEFAULT_TIMEOUT_BY_SIZE` / `TIMEOUT_BOUNDS_BY_SIZE` 按 size 控制 |
| 无 I/O stamp 时软一次 | ✅ Pass | `classify_timeout()` 中 `last_io_at is None` → 返回 `"soft"` |
| Cronner/Execution 钩子 | ✅ Pass | `lifecycle.py:3694` 和 `execution.py:1369` 均调用 `try_soft_timeout_extension` |

---

## 主线证据

### 2.1 自动化测试结果

**命令：** `python3 -m pytest tests/test_timeout_policy.py -q`

**Exit code：** 0

**输出：**
```
.......                                                                  [100%]
7 passed in 0.18s
```

**对应用例名与覆盖：**

| 用例名 | 测试内容 | 对应验收 |
|--------|---------|---------|
| `test_classify_awaiting_model_is_soft` | `io_phase="awaiting_model"` → `"soft"` | I/O 显式态 → soft |
| `test_classify_stale_idle_is_hard` | `io_phase="idle"`, 1000s 无 I/O → `"hard"` | hard idle 判定 |
| `test_classify_missing_last_io_is_soft_once` | `last_io_at=None` → `"soft"` | 首次无 I/O stamp 软一次 |
| `test_default_and_double_budgets` | `default_timeout_seconds` / `next_soft_timeout_seconds` / `clamp_timeout_seconds` | 体量预算 + 翻倍 + 钳位 |
| `test_derive_io_phase_from_events` | `derive_io_phase()` 从 stdout 析取 `io_phase` / `last_io_at` | I/O 态提取 |
| `test_soft_timeout_extends_and_caps` | 完整续命：延长 → 台账 → `soft_timeout_count` 递增 → `MAX_SOFT_TIMEOUTS` 后返回 `soft_cap_reached` | soft≤3×2 + 台账 + 封顶 |
| `test_hard_idle_does_not_extend` | hard idle 场景 → `hard_idle`，`extend_provider_job_timeout` 未调用 | hard idle 不翻倍 |

### 2.2 关键路径静态证据

#### classify_timeout — I/O 显式态与 soft/hard 判定

**文件：** `plowwhip/timeout_policy.py`

- `ACTIVE_IO_PHASES = frozenset({"awaiting_model", "model_response", "tool", "compacting"})` — 定义活跃 I/O 态集合
- `classify_timeout()` 第 1 判：若 `io_phase` 在 `ACTIVE_IO_PHASES` → `"soft"`
- 第 2 判：若 `io_phase in {"failed", "completed"}` → `"hard"`
- 第 3 判（LIVE-DS-22 / R1）：若 `last_io_at is None` → `"soft"`（无 I/O stamp 时软一次）
- 第 4 判：若 `idle_for >= IDLE_HARD_SECONDS(300)` → `"hard"`；否则 `"soft"`
- `IDLE_HARD_SECONDS = 300` — hard idle 判据阈值

#### soft 续命逻辑 — try_soft_timeout_extension

**文件：** `plowwhip/soft_timeout.py`

- 跳过条件：`status` 不在活跃集；`deadline_reached_at` 为空且 deadline 未到 → `"skipped"`
- hard 分支：返回 `"hard_idle"`，写入 `task_timeout_hard` 事件，台账注记「hard idle — 无近期模型/工具 I/O」
- soft_cap 分支：`soft_timeout_allowed(soft_count)` 为 False → 返回 `"soft_cap_reached"`，写入 `task_timeout_soft_cap` 事件
- extend 分支：调用 `extend_provider_job_timeout(job_id, next_budget)` → 更新 `dispatch_json`（`soft_timeout_count++`、`timeout_seconds=next_budget`）→ `write_task_fields` 更新 `deadline_at` → 写入 `task_timeout_soft_extended` 事件 → Butler 消息 → 台账
- `MAX_SOFT_TIMEOUTS = 3`（`timeout_policy.py`）
- `next_soft_timeout_seconds()` 翻倍：`max(MIN_TIMEOUT_SECONDS, current) * 2`，再经 `clamp_timeout_seconds` 按 size 钳位

#### I/O 相提取 — derive_io_phase

**文件：** `plowwhip/io_phase.py`

- 从 HostJob stdout 事件流析取最新 `model.request_started` / `tool.progress` 等事件
- 返回 `{"io_phase": ..., "last_io_at": ..., "last_event": ..., "failure_class": ...}`
- 被 `host_bridge.py` 的 `_enrich_io_phase()` 和 `get_job_status()` 调用

#### extend_timeout — Host Bridge 续命入口

**文件：** `plowwhip/host_bridge.py:584`

```
def extend_timeout(self, value: object, timeout_seconds: int) -> dict[str, object]:
    """Butler soft-timeout: raise wall budget; do not kill a live HostJob."""
    job_id = _job_id(value)
    bounded = min(max(int(timeout_seconds), 10), MAX_JOB_SECONDS)
    ...
    record["timeout_seconds"] = bounded
    record["deadline_reached_at"] = None
    record["timeout_extended_at"] = time.time()
```

- 路径 `/v1/jobs/extend_timeout`（`host_bridge.py:168`）通过 `manager.extend_timeout()` 处理
- 调用方：`extend_provider_job_timeout()`（`soft_timeout.py` import）

#### Cronner / Lifecycle 软超时钩子

**文件：** `plowwhip/lifecycle.py:3679-3708`

```python
# LIVE-DS-22: soft budget miss → butler ≤3×2; hard idle → reconcile/stop.
from .soft_timeout import try_soft_timeout_extension
...
outcome = try_soft_timeout_extension(connection, task, active, state, now=now)
if outcome == "extended":
    return "soft_timeout_extended"
```

**文件：** `plowwhip/execution.py:1367-1375`

```python
from .soft_timeout import try_soft_timeout_extension
soft_outcome = try_soft_timeout_extension(connection, task, job, state, now=now)
if soft_outcome == "extended":
    return "wait"
if soft_outcome in {"hard_idle", "soft_cap_reached"}:
    ...  # 设 timeout_stage="reconcile"，后续进入 reconcile 路径
```

---

## 机制验证

### 3.1 soft ≤ 3×2 翻倍路径

**触发条件：** `io_phase` ∈ `ACTIVE_IO_PHASES` 或 idle 未超 300s，`deadline_reached_at` 不为空，`soft_count < MAX_SOFT_TIMEOUTS(3)`

**行为链：**
1. `extend_provider_job_timeout(job_id, next_budget)` — 通知 Host Bridge 续命
2. `dispatch_json.soft_timeout_count` 递增
3. `dispatch_json.timeout_seconds` → next_budget（翻倍）
4. `tasks.deadline_at` → `current + next_budget`
5. `tasks.wait_reason` → 含「预算 previous→next_budget；模型仍在推进」
6. Butler 消息 + 台账追加
7. `task_timeout_soft_extended` 事件写入

**测试覆盖：** `test_soft_timeout_extends_and_caps`（延长 → 计数 → 台账 → 封顶）

### 3.2 hard idle 不翻倍路径

**触发条件：** `io_phase` 不在活跃集且非 completed/failed；`last_io_at` 存在且 `idle_for ≥ 300s`

**行为链：**
1. 不调 `extend_provider_job_timeout` → 不翻倍
2. Butler 消息：「不翻倍续命（io_phase=idle）」
3. 台账注记：「hard idle — 无近期模型/工具 I/O」
4. `task_timeout_hard` 事件写入
5. 返回 `"hard_idle"`，Cronner 转为 reconcile/stop

**测试覆盖：** `test_hard_idle_does_not_extend`

### 3.3 台账留痕

**文件：** `plowwhip/timeout_policy.py` — `ledger_timeout_note()` 与 `append_timeout_ledger()`

- 格式：`"- LIVE-DS-22 / soft-timeout: Task ... class=... attempt=... budget ...; io_phase=...; 结论：Task 超时阈值判断有误（reason）。"`
- 现场记录已存在于 `docs/BASELINE_V2_REMEDIATION_LEDGER.zh-CN.md` 的 `## LIVE-DS-22 超时续命现场记录` 章节

### 3.4 体量感知超时门限

**文件：** `plowwhip/timeout_policy.py`

```python
DEFAULT_TIMEOUT_BY_SIZE = {"simple": 600, "medium": 1800, "large": 3600}
TIMEOUT_BOUNDS_BY_SIZE = {"simple": (120, 3600), "medium": (300, 14400), "large": (600, 86400)}
```

- `default_timeout_seconds(size)` 按 size 返回默认预算
- `clamp_timeout_seconds(value, size)` 按 size 钳位
- `next_soft_timeout_seconds(current, size)` 翻倍后仍按 size 钳位

**测试覆盖：** `test_default_and_double_budgets`

---

## 风险与建议

### 风险 R1：extend_provider_job_timeout 异常处理

`try_soft_timeout_extension()` 中 `extend_provider_job_timeout` 若抛 `OSError/ValueError/RuntimeError` 返回 `"extend_failed"`。目前 `extend_failed` 返回后，调用方（`execution.py:1369` 和 `lifecycle.py:3694`）的处理为：

- `execution.py`：若返回值不是 `"extended"` 且不是 hard_idle/soft_cap_reached，无专门分支
- `lifecycle.py`：相同逻辑

建议补充 `extend_failed` 的显式 fallback 逻辑（如重试一次或降级为硬超时）。**当前风险等级：P2。**

### 风险 R2：repo_root 为 None 时的台账回退

`_append_ledger` 中若 `repo_root is None`，回退到 `Path(__file__).resolve().parents[1]`。在容器化部署（如 `WORKDIR=/app`）时预期正确，但若项目结构变更可能导致台账写入到错误路径。**当前风险等级：P3。**

### 风险 R3：hard idle 判定仅依赖 `last_io_at` 时间戳

若 `last_io_at` 在 Provider 侧因时钟偏差不准确，可能导致误判。当前无跨节点时钟同步保障。**当前风险等级：P3。**

---

## 结论

**LIVE-DS-22 回归通过。**

- 7/7 自动化测试全部通过，覆盖 soft/hard 判定、续命翻倍、封顶、台账留痕、I/O 态提取
- 关键路径（`classify_timeout` → `try_soft_timeout_extension` → `extend_provider_job_timeout` → Butler 消息 + 台账）完整可追溯
- `execution.py` 和 `lifecycle.py` 中 Cronner 软超时钩子已就位
- 缺口：R1（`extend_failed` 无显式 fallback）可后续加固，不影响当前回归通过判定
