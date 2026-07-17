# Sprint 10 — Budget Single Truth Result

## Objective

修复 B2 人工预算覆盖的双预算真相：覆盖后 `token_budget` 与 `execution_budget.total_token_hard_cap` 必须一致，原估算基线以 `estimated_total_token_hard_cap` 审计保留。

## Problem

人工覆盖 `token_budget=300000` 时，持久化的 `execution_budget.total_token_hard_cap` 仍为估算值 225000，形成双预算真相。

## Fix (`backend/plow_whip_web/api/app.py`)

- **无覆盖**：`total_token_hard_cap` = 估算值；`execution_budget` 不含 `estimated_total_token_hard_cap`
- **有覆盖**：
  - `estimated_total_token_hard_cap` = 原估算 hard cap（审计基线）
  - `total_token_hard_cap` = 实际 `token_budget`（单一执行真相）
  - soft/hard deadline、max_turns、attempts、verification timeout、reserved_tokens 不变
- **拒绝**：`token_budget < reserved_tokens` → HTTP 400，`code=token_budget_below_reserved`
- repository `task.created` 事件摘要读取更新后的 `execution_budget.total_token_hard_cap`（实际 cap）

## Test Evidence

| 场景 | 断言 |
|------|------|
| 无覆盖 | `total_token_hard_cap=225000`，无 `estimated_total_token_hard_cap` |
| 人工覆盖 | `token_budget == execution_budget.total_token_hard_cap == 300000`，`estimated_total_token_hard_cap == 225000`，deadline 不变 |
| 低于 reserved | S 档 `reserved=60000`，`token_budget=50000` → `token_budget_below_reserved` |
| 创建事件 | `total_token_hard_cap == 300000`（实际 cap） |

```text
$ .venv/bin/python -m pytest tests/test_budget_policy.py -q
......................                                                   [100%]
22 passed

$ git diff --check
(no output — clean)
```

## Files Touched (allowed scope only)

- `backend/plow_whip_web/api/app.py`
- `tests/test_budget_policy.py`
- `docs/SPRINT_10_BUDGET_SINGLE_TRUTH_RESULT.md`

SPRINT10_BUDGET_SINGLE_TRUTH_COMPLETE
