# Sprint 10 — TaskSizing Create API Result

## Objective

接通 0 Token 预估 → 创建 API → TaskView：创建时服务端重新计算 sizing，持久化 estimated/legacy_fallback 事实，TaskView 暴露全部 budget 字段。不改 migration、Repository 持久化实现、调度/Host/Deadline 或前端。

## Actual Diff (this slice)

### `backend/plow_whip_web/api/schemas.py`

- `TaskCreate` 新增 `sizing_inputs: TaskSizingEstimateRequest | None`、`manual_override_reason`
- `quality_profile` 接受 `fast|balanced|strict|deterministic`，validator 一律规范化为 `deterministic`
- `TaskView` 暴露 `sizing`、`execution_budget`、`manual_override`、`override_reason`、`budget_overrun_evidence`

### `backend/plow_whip_web/api/app.py`

- `create_task` 有 `sizing_inputs` 时调用 `estimate_task_sizing()` 重新计算（不信任客户端预览）
- `needs_planning` → HTTP 400，`detail.code=needs_planning`，含 `missing_gates`
- `estimated` 时 `_preview_to_persistence()` 拆成 sizing + execution_budget 传给 `TaskRepository.create`
- 默认 `token_budget = total_token_hard_cap`（M 档示例：225000）
- `token_budget != hard_cap` 且无 `manual_override_reason` → HTTP 400 `manual_override_required`
- 有人工原因时标记 `manual_override=True`，deadline 字段保持 policy 原值不变
- 无 `sizing_inputs` 的旧 UI 路径继续创建，repository 写入显式 `legacy_fallback`

## Regression Coverage

| # | 断言 | 测试 |
|---|------|------|
| 1 | estimate 输入 → create 持久化完全一致 | `test_create_with_sizing_inputs_matches_estimate_preview` |
| 2 | 缺 gate 拒绝且含 missing_gates | `test_create_rejects_missing_gates_with_machine_readable_detail` |
| 3 | 伪造 token_budget 无原因拒绝 | `test_create_rejects_forged_token_budget_without_override_reason` |
| 4 | 带原因覆盖被记录、deadline 不变 | `test_create_records_manual_override_without_changing_deadlines` |
| 5 | 旧 UI fast 成功 → deterministic + legacy_fallback | `test_legacy_ui_fast_request_stores_deterministic_and_legacy_fallback` |
| 6 | TaskView 返回全部事实 | create + GET 往返测试 |
| 7 | estimate/create 不新增 token_usage | `test_create_and_estimate_do_not_record_token_usage` |
| 8 | 幂等 replay 不覆盖首次事实 | `test_api_idempotent_create_does_not_overwrite_sizing_facts` |

M 档派发前预判（layers=2、components=3、files=4、migration=false、risk=medium、四 gate 齐全）：
`reserved=150000`、`soft=480s`、`hard=1200s`、`hard_cap=225000`。

## Test Evidence

```text
$ .venv/bin/python -m pytest tests/test_budget_policy.py -q
.....................                                                    [100%]
21 passed

$ git diff --check
(no output — clean)
```

## Files Touched (allowed scope only)

- `backend/plow_whip_web/api/schemas.py`
- `backend/plow_whip_web/api/app.py`
- `tests/test_budget_policy.py`
- `docs/SPRINT_10_SIZING_CREATE_RESULT.md`

SPRINT10_SIZING_CREATE_COMPLETE
