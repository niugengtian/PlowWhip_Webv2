# Sprint 10 — TaskSizing Preview Result

## Objective

实现统一 TaskSizing 的创建前预估：`POST /api/tasks/estimate`，完全确定性、0 模型 Token，不做持久化/调度/Host Bridge/前端改动。

## Actual Diff (this slice)

### 新增 `backend/plow_whip_web/runtime/sizing.py`

- 纯函数 `estimate_task_sizing(TaskSizingInputs) -> dict`
- `clamp_total_token_hard_cap(total_p90)`：`p90 总和 × 1.5`，夹在 25k–1.5M
- Bootstrap 冷启动表（input/output 75/25 拆分）：

| size_class | p90 total | soft | hard | max_turns | max_attempts | verification_timeout | progress_extension |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| XS | 25k | 120 | 300 | 10 | 2 | 60 | 60 |
| S | 60k | 240 | 600 | 20 | 2 | 120 | 90 |
| M | 150k | 480 | 1200 | 40 | 3 | 300 | 120 |
| L | 400k | 900 | 2400 | 80 | 3 | 600 | 180 |
| XL | 800k | 1800 | 4800 | 120 | 4 | 900 | 300 |

- 复杂度分映射：`XS <25 | S 25–59 | M 60–119 | L 120–199 | XL ≥200`
- 四项 dispatch gate 任一缺失 → `needs_planning`，预算字段全部为 `null`
- `rationale` 列出各结构化加权项

### `backend/plow_whip_web/api/schemas.py`

- 新增 `TaskSizingEstimateRequest`（`extra=forbid`，拒绝 title/objective 等非结构化字段）
- 新增 `TaskSizingEstimateResponse` / `TokenEstimateBand`
- 现有 `TaskCreate` 路径未改动行为（保留工作树已有脏改动）

### `backend/plow_whip_web/api/app.py`

- 新增 `POST /api/tasks/estimate` → 调用 `estimate_task_sizing`，无 DB 写入

## Regression Coverage

| # | 断言 | 测试 |
|---|------|------|
| 1 | 四项 gate 缺失 → needs_planning | `test_missing_dispatch_gates_*`, `test_all_four_gates_*` |
| 2 | 同输入稳定同输出 | `test_same_structured_inputs_*`, API 重复 POST |
| 3 | M/L 档映射 | `test_medium_and_large_size_class_mapping` |
| 4 | hard cap 最小/最大 | `test_total_token_hard_cap_clamps_*` |
| 5 | 标题/目标不影响估算 | `test_estimate_api_rejects_unstructured_title_fields` (422) |
| 6 | model_invoked=false，无 token_usage | `test_estimate_api_is_deterministic_*` |

## Test Evidence

```text
$ .venv/bin/python -m pytest tests/test_budget_policy.py -q
........                                                                 [100%]
8 passed

$ git diff --check
(no output — clean)
```

## Files Touched (allowed scope only)

- `backend/plow_whip_web/api/schemas.py`
- `backend/plow_whip_web/api/app.py`
- `backend/plow_whip_web/runtime/sizing.py` (new)
- `tests/test_budget_policy.py` (new)
- `docs/SPRINT_10_SIZING_PREVIEW_RESULT.md`

SPRINT10_SIZING_PREVIEW_COMPLETE
