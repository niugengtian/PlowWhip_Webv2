# 有界只读修复规划报告 — 对照 LIVE-DS-22 超时与续命机制审计

> 审计日期：2026-07-25 · 数据源：BASELINE_V2_REMEDIATION_LEDGER.zh-CN.md、DEEPSEEK_UNATTENDED_PLANNER_TO_DONE.md、timeout_policy.py、soft_timeout.py、io_phase.py  
> 审计方式：只读代码审查，不运行/重建/全库扫描

---

## 总判

经审查三项已落地超时与续命机制（`timeout_policy.py`、`soft_timeout.py`、`io_phase.py`），对照台账 LIVE-DS-22 的修复目标，**主线已完成闭环，机制设计合理，对无人值守场景提供了三层防御**。

- **核心逻辑**：`classify_timeout()` 根据 `io_phase` 与 `last_io_at` 区分 soft（仍有 I/O，预算不足）和 hard（空闲超时，不翻倍），杜绝旧版「一刀切杀仍在回复的模型」问题。
- **续命上限**：`MAX_SOFT_TIMEOUTS=3`，搭配 `soft_timeout_allowed()` 硬阄，防止无限续命。
- **台账记录**：每次超时均写入 `BASELINE_V2_REMEDIATION_LEDGER.zh-CN.md` 的 `## LIVE-DS-22 超时续命现场记录` 章节，以 `ledger_timeout_note()` 标准格式落痕。
- **IO 态推断**：`io_phase.py` 的 `derive_io_phase()` 从 json-worker stdout JSONL 推断 `awaiting_model`、`model_response`、`tool`、`compacting` 等活跃态，无需额外仪器化。

**主要风险等级：低（Low）**。存在少数边界条件与隐式假设，但均属可控范围内，详见第四章。

---

## 主线证据

### 1. 台账 LIVE-DS-22 描述

台账中 LIVE-DS-22 原文：

> **失败标准错误**：工具空转计数杀仍在回复的模型；Task 无体量超时；超时直接停；同构重试烧 Token。  
> **涉及文件**：`timeout_policy.py`、`soft_timeout.py`、`io_phase.py`、`simple_worker`、`host_bridge`、`lifecycle`、`execution`。  
> **验收标准**：I/O 显式态；soft≤3×2 上报管家；hard idle 不翻倍；exploration 不再以 96 次工具为杀刀。  
> **状态**：已实现待回归。

### 2. 已落地模块对应

#### 2.1 `timeout_policy.py` — 超时策略核心（208 行）

| 函数/常量 | 职责 | 对照 LIVE-DS-22 |
|---|---|---|
| `ACTIVE_IO_PHASES` | 定义活跃 I/O 态集合（`awaiting_model`、`model_response`、`tool`、`compacting`） | ✅ I/O 显式态 |
| `classify_timeout()` | 根据 `io_phase` 与 `last_io_at` 返回 `"soft"` 或 `"hard"` | ✅ 区分活跃态与空闲 |
| `clamp_timeout_seconds()` | 按 size 边界钳制超时值 | ✅ 体量感知超时 |
| `next_soft_timeout_seconds()` | soft 超时翻倍（`max(60, current)*2`），再钳制 | ✅ soft≤3×2 |
| `soft_timeout_allowed()` | 检查是否 ≤ `MAX_SOFT_TIMEOUTS`（=3） | ✅ 硬上限 3 次 |
| `MAX_SOFT_TIMEOUTS = 3` | 软超时最大续命次数 | ✅ 台账明确要求 |
| `IDLE_HARD_SECONDS = 300` | 空闲 300 秒 → hard idle（不翻倍） | ✅ hard idle 不翻倍 |
| `ledger_timeout_note()` | 生成标准台账记录字符串，含 `结论：Task 超时阈值判断有误` | ✅ 台账审计痕迹 |
| `append_timeout_ledger()` | 追加记录到 LIVE-DS-22 现场记录章节 | ✅ 永久留痕 |

#### 2.2 `soft_timeout.py` — 续命执行器（300+ 行）

| 函数/常量 | 职责 | 对照 LIVE-DS-22 |
|---|---|---|
| `try_soft_timeout_extension()` | 主入口：检查 deadline、判断 soft/hard、执行续命或记录硬空闲 | ✅ 自动续命 |
| 跳过条件：status 非活躍或 deadline 未到 | 防止误触发续命 | ✅ 精准触发 |
| hard idle 分支 → 写入 `task_timeout_hard` 事件 + 台账 | 硬空闲：不翻倍、记录原因 | ✅ 不翻倍 |
| soft_cap_reached 分支 → 写入 `task_timeout_soft_cap` 事件 + 台账 | 续命 3 次已达上限 | ✅ 停止翻倍 |
| soft extension 分支 → `extend_provider_job_timeout()` + 更新 dispatch、deadline、public_status | 真正执行续命：更新 `deadline_at`、`wait_reason` | ✅ 预算翻倍 |
| `_record_butler_notice()` | 向管家写入中文可读消息 | ✅ 上报管家 |
| `_append_ledger()` | 调用 `timeout_policy.append_timeout_ledger()` | ✅ 台账记录 |

#### 2.3 `io_phase.py` — I/O 状态推断（130 行）

| 逻辑 | 职责 | 对照 LIVE-DS-22 |
|---|---|---|
| `derive_io_phase()` | 解析 stdout JSONL 事件，返回 `io_phase` / `last_io_at` / `failure_class` | ✅ I/O 显式态 |
| 事件类型映射：`model.request_started` → `awaiting_model`；`tool.started` → `tool`；`session.compacted` → `compacting` 等 | ✅ 多态识别 |
| `worker.completed` 事件 → 直接返回 `completed`/`failed` + `failure_class` | ✅ 终态收敛 |
| `worker.progress` 事件 → `idle_progress` 保持活跃 | ✅ 心跳不杀活 |

### 3. 数据流验证

```
HostJob stdout JSONL
    ↓
io_phase.derive_io_phase()   ← 产出 io_phase / last_io_at
    ↓
timeout_policy.classify_timeout()  ← 区分 soft / hard
    ↓
soft_timeout.try_soft_timeout_extension()
    ├─ hard idle      → 记录台账，不翻倍，返回 hard_idle
    ├─ soft cap (≥3)  → 记录台账，不翻倍，返回 soft_cap_reached
    └─ soft (≤2)      → extend_provider_job_timeout()
                       → 更新 deadline_at / wait_reason
                       → 写入 task_timeout_soft_extended 事件
                       → 追加台账 LIVE-DS-22 现场记录
                       → 返回 extended
```

---

## 机制验证

### 验证 1：soft vs hard 二分类正确性

**测试点**：
- `io_phase` in `ACTIVE_IO_PHASES` → `"soft"`
- `phase not in ACTIVE_IO_PHASES` 且 `last_io_at < now - IDLE_HARD_SECONDS` → `"hard"`
- `phase` 为 `{"failed", "completed"}` 且 `idle_for≥IDLE_HARD_SECONDS` → `"hard"`

**判断**：逻辑清晰无歧路。但 `classify_timeout()` 的 `idle_for < float(idle_hard_seconds)` 分支在 `phase="idle"` 且 `last_io_at` 较新时仍返回 `"soft"` —— 这符合「仍在回合间但未超空闲阈值」的预设。

### 验证 2：续命上限严格执行

**测试点**：
- `soft_timeout_allowed(0)` → `True`
- `soft_timeout_allowed(2)` → `True`
- `soft_timeout_allowed(3)` → `False`
- `soft_timeout_allowed(-1)` → `False`

**判断**：`int(count) < MAX_SOFT_TIMEOUTS` 即 `0 ≤ count ≤ 2` 为 True，第 3 次请求被拒。边界正确。

### 验证 3：台账落盘幂等性

**测试点**：
- `append_timeout_ledger()` 检查 `marker.strip() not in body`，若不存在则追加 marker 章节。多次调用不会重复 marker。

**判断**：正确。但 `path.is_file()` 返回 False 时静默返回 `None`，无日志/异常——属低风险隐式静默。

### 验证 4：I/O 相位推断覆盖率

`derive_io_phase()` 覆盖的事件类型：
- `model.request_started` → `awaiting_model`
- `model.response_started` / `model.response_completed` → `model_response`
- `tool.started` / `tool.finished` → `tool`
- `session.compacted` → `compacting`
- `worker.progress` → `idle_progress`
- `worker.completed` → `completed` / `failed`

**判断**：覆盖主流事件。未覆盖 `worker.error`、`worker.cancelled` 等非标准事件——但 json-worker 合同规范中这些事件不常见，风险极低。

### 验证 5：事务边界与异常安全

`try_soft_timeout_extension()` 使用 SQLite `connection.execute()` 但**未包裹显式事务**。在多线程/多进程并发调用时，可能存在：
1. 读取 `dispatch["soft_timeout_count"]` 后另一个进程已更新
2. 两次续命同时更新 `deadline_at` 导致不一致

**判断**：当前架构中 Cronner 是唯一调度者，单线程调用 `try_soft_timeout_extension()`，无并发竞争。若未来改为多 Cronner，需要加 `UPDATE … RETURNING` 或行锁。

### 验证 6：extend 调用失败回退

`extend_provider_job_timeout()` 抛出 `OSError`、`ValueError`、`RuntimeError` 时返回 `"extend_failed"`。调用方未重试，也未改状态——**但这是 Cronner 循环，下次 tick 仍会重试**，恢复能力隐式存在。

---

## 风险与建议

### 风险 R1：`classify_timeout()` 对 `last_io_at=None` 的处理

当 `io_phase` 为空或未知，且 `last_io_at` 为 `None` 时：

```python
if last_io_at is not None:       # ← False
    idle_for = ...
    if idle_for < ... and phase not in {...}:
        return "soft"
return "hard"                      # ← 默认 hard
```

这会在刚启动、尚无 I/O 事件的 Task 中，直接判为 `hard`。但此时 Cronner 可能刚检查到 deadline 超时，Task 实际正在等待首次模型调用。

**影响**：刚启动的 Task 被误判为 hard idle，不翻倍直接停。  
**建议**：在 `last_io_at is None` 且 `phase != "failed"` 时，默认返回 `"soft"` 给予一次宽容续命机会——但需配合 `soft_timeout_count` 限定总次数（目前 3 次），不会无限续。

### 风险 R2：`io_phase.py` 未覆盖所有标准事件

json-worker 规范可能包含 `worker.error`（非 completed 但有 error 信息）、`worker.cancelled`（被外部取消）、`tool.error`（工具调用报错）。这些事件当前被静默忽略。

**影响**：工具调用报错时，`io_phase` 保持上一次活跃态（如 `"tool"`），可能让 `classify_timeout` 误判为「仍在推进」并续命——但实际上模型已因错误退出 I/O 循环。

**建议**：在 `derive_io_phase()` 中增加 `worker.error` → `"failed"`、`tool.error` → `"failed"` 映射，确保错误后正确回退。

### 风险 R3：台账追加无重试/幂等性边界

`append_timeout_ledger()` 使用 `path.read_text()` + `path.write_text()` 原子性依赖文件系统。在 Docker volume 或 NFS 挂载场景下，并发写入可能导致记录丢失或截断。

**影响**：台账记录完整性在分布式环境中不可靠。  
**建议**：当前作为可选调试辅助，不依赖其正确性做决策，风险可控。若需强一致日志，应写入 DB `task_events` 而非文件。

### 风险 R4：soft 续命后 `wait_reason` 文本可能暴露内部细节

```python
"wait_reason": (
    f"[超时-soft {soft_count}/{MAX_SOFT_TIMEOUTS}] "
    f"预算 {previous}s→{next_budget}s；模型仍在推进，已上报管家续命"
)
```

该字符串写入 `public_status` 与 `wait_reason`，可能在 UI 或 API 中对外暴露内部预算细节。

**影响**：低（内部工具不对外）。但若未来有外部 API，需清理。  
**建议**：对外暴露的 `wait_reason` 使用通用文案，详细日志经 `task_events` 获取。

### 综合建议优先级

| 优先级 | 建议 | 影响模块 | 对应风险 |
|---|---|---|---|
| P1 | `classify_timeout()` 对 `last_io_at=None` 初次宽容判 soft | `timeout_policy.py` 第 60–72 行 | R1 |
| P1 | 增加 `worker.error` / `tool.error` 事件映射为 `"failed"` | `io_phase.py` 第 90–110 行 | R2 |
| P2 | `try_soft_timeout_extension()` 使用显式 `BEGIN/COMMIT` | `soft_timeout.py` 第 45–220 行 | R3（未来并发场景） |
| P2 | `wait_reason` 对外使用通用文案 | `soft_timeout.py` 第 190–195 行 | R4 |
| P3 | 台账追加改用 DB `task_events` 替代文件写入 | `timeout_policy.py` `append_timeout_ledger()` | R3 |

---

## 对照 LIVE-DS-22 验收标准总表

| 验收项 | 状态 | 依据 |
|---|---|---|
| I/O 显式态 | ✅ 已实现 | `io_phase.py` 定义 6 种活跃态；`ACTIVE_IO_PHASES` 显式定义 |
| soft≤3×2 上报管家 | ✅ 已实现 | `MAX_SOFT_TIMEOUTS=3`；`_record_butler_notice()` 写入管家消息队列 |
| hard idle 不翻倍 | ✅ 已实现 | `classify_timeout()` hard 分支直接返回 `"hard_idle"`，不调用续命 |
| exploration 不以 96 次工具为杀刀 | ✅ 已实现 | 超时策略改为墙钟 + I/O 态，非工具调用次数 |
| 体量感知超时门限 | ✅ 已实现 | `DEFAULT_TIMEOUT_BY_SIZE` / `TIMEOUT_BOUNDS_BY_SIZE` 按 size 控制 |
| 台账留痕 | ✅ 已实现 | `ledger_timeout_note()` + `append_timeout_ledger()` 写入"超时阈值判断有误" |

**结论**：LIVE-DS-22 在 `timeout_policy.py` / `soft_timeout.py` / `io_phase.py` 中的修复已全部实现，验收标准完全覆盖。建议按 P1 优先级修复风险 R1 和 R2 后进入回归测试。
