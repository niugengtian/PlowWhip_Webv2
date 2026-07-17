# Sprint 10 — ExecutionBudget Runtime Boundary Result

## Objective

将已持久化的 `execution_budget` 接入真实执行时间边界：Provider/Host job 派发、轮询 lease 续期、任务锁/lease 均以 `hard_deadline_seconds` 为唯一 deadline 来源；`legacy_fallback` 显式退回 `command.timeout_seconds`。本切片不改 UI、估算档位、部署或 schema/domain/migration。

## 统一机制

| 路径 | estimated | legacy_fallback |
|------|-----------|-------------------|
| Host job `timeout_seconds` | `execution_budget.hard_deadline_seconds` | `command.timeout_seconds`（默认 600） |
| 任务 lease / resource lock | `hard_deadline + 60s grace`（下限 300s） | 同上，基于 command timeout |
| Host job 轮询 renew | `task_lease_seconds(task)` | 同上 |
| claim token reservation | `execution_budget.reserved_tokens` | 现有 `BudgetManager.host_reservation()`（剩余 budget） |

安全总上限：`MAX_HARD_DEADLINE_SECONDS = 4800`（与 sizing XL 档一致）。`soft_deadline_seconds` 本切片未接入失败终态。

新增 helpers（`store/task_repository.py`）：

- `task_hard_deadline_seconds(task)`
- `task_lease_seconds(task)`
- `task_host_reserved_tokens(task, remaining=...)`

## 变更文件

- `backend/plow_whip_web/store/task_repository.py` — deadline/lease/reservation helpers；claim lease 改用 `task_lease_seconds`
- `backend/plow_whip_web/providers/pool.py` — `execute_task` / `start_task_job` 使用 `task_hard_deadline_seconds`
- `backend/plow_whip_web/providers/host_bridge.py` — 移除固定 620s HTTP 截断；客户端上限改为 4800+20s
- `backend/plow_whip_web/runtime/task_service.py` — estimated 使用 `reserved_tokens`；active job renew 使用 lease helper
- `tests/test_provider_pool.py` — deadline helper、M/L/XL/legacy Host 派发断言
- `tests/test_host_job_continuity.py` — estimated reservation 150000、legacy command timeout、续接/恢复回归

## 测试证据

```bash
.venv/bin/python -m pytest tests/test_provider_pool.py tests/test_host_job_continuity.py -q
# 31 passed

git diff --check
# (no whitespace errors)
```

| 断言 | 测试 |
|------|------|
| M 档 1200s 原样进入 Host | `test_provider_pool_passes_execution_budget_hard_deadline_to_host`, `test_estimated_host_dispatch_uses_execution_budget_deadline_and_reservation` |
| L/XL 不被 600/620 截断且 ≤4800 | `test_provider_pool_passes_execution_budget_hard_deadline_to_host` |
| legacy 仍用 command timeout | `test_execution_deadline_helpers_use_budget_or_legacy_command`, `test_legacy_host_dispatch_keeps_command_timeout_and_remaining_reservation` |
| lease 覆盖 hard deadline + grace | `test_estimated_host_dispatch_uses_execution_budget_deadline_and_reservation` |
| estimated reservation = 150000 | `test_estimated_host_dispatch_uses_execution_budget_deadline_and_reservation` |
| 恢复/续接复用 session、不多算 attempt | 既有 `test_interrupted_host_job_*`, `test_timeout_completed_snapshot_*` 仍通过 |

## 已知边界

- Host Bridge **服务端**（`backend/plow_whip_web/host_bridge.py`）仍对 job payload 做 3600s 上限；本切片仅保证客户端不再以 620/600 提前截断。L(2400)/XL(4800) 在客户端侧已正确传递；服务端放宽需后续切片。
- `soft_deadline_seconds` 仅持久化，尚未接入进度检查或失败判定。

SPRINT10_EXECUTION_BUDGET_RUNTIME_COMPLETE
