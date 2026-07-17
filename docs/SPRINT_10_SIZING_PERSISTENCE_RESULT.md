# Sprint 10 — TaskSizing/ExecutionBudget Persistence Result

## Objective

为 tasks 建立单一、可恢复的 sizing/execution budget 事实：持久化 `sizing_json`、`execution_budget_json`、`manual_override`、`override_reason`、`budget_overrun_evidence_json`；`TaskRecord` 明确暴露；历史/旧调用显式标记 `legacy_fallback`。本切片不接 API、不改调度、Host、前端或 Deadline 执行。

## Actual Diff (this slice)

### 新增 `backend/plow_whip_web/store/migrations/0016_task_sizing_budget.sql`

```sql
ALTER TABLE tasks ADD COLUMN sizing_json TEXT;
ALTER TABLE tasks ADD COLUMN execution_budget_json TEXT;
ALTER TABLE tasks ADD COLUMN manual_override INTEGER NOT NULL DEFAULT 0 CHECK (manual_override IN (0, 1));
ALTER TABLE tasks ADD COLUMN override_reason TEXT;
ALTER TABLE tasks ADD COLUMN budget_overrun_evidence_json TEXT;

UPDATE tasks SET sizing_json = '{"status":"legacy_fallback"}' WHERE sizing_json IS NULL;
```

历史任务在迁移时被显式回填为 `legacy_fallback`，不会伪装成 estimated。

### `backend/plow_whip_web/domain/model.py`

`TaskRecord` 新增 5 个字段：

```python
sizing: dict[str, Any]
execution_budget: dict[str, Any] | None
manual_override: bool
override_reason: str | None
budget_overrun_evidence: dict[str, Any] | None
```

### `backend/plow_whip_web/store/task_repository.py`

- `create()` 新增 5 个可选参数并原样保存（使用现有 `_dump` 统一序列化）：
  - `manual_override=True` 且 `override_reason` 为空/空白 → 拒绝（`DomainError`）
  - 非人工路径传入 `override_reason` → 拒绝，不允许伪造覆盖原因
  - `sizing=None`（旧调用）→ 显式写入 `{"status": "legacy_fallback"}`；此时禁止单独传 `execution_budget`
- `task.created` 事件只放低成本摘要：`sizing_status`、`size_class`、`bootstrap_version`、`total_token_hard_cap`、`hard_deadline_seconds`、`manual_override`；不重复塞大 JSON（无 rationale/token 区间）
- `_task_from_row()` 还原全部新字段；`sizing_json` 为 NULL 的行读出即 `legacy_fallback`

未修改 `api/schemas.py`、`api/app.py`、`runtime/sizing.py`、`runtime/budget.py`、`task_service.py`、`host_bridge.py`、settings、`web/**`、0015 migration；保留工作树全部已有脏改动。

## Regression Coverage (tests/test_budget_policy.py 追加 6 个)

| # | 断言 | 测试 |
|---|------|------|
| 1 | estimated sizing+budget round-trip（M 档 150000/480/1200/225000） | `test_estimated_sizing_and_budget_round_trip_across_reopen` |
| 2 | list/get 与重开 Database 后一致 | 同上（重建 `Database`+`TaskRepository` 后读取） |
| 3 | idempotent create 不覆盖原事实 | `test_idempotent_create_does_not_overwrite_original_sizing` |
| 4 | legacy 调用得到显式 legacy_fallback | `test_legacy_create_call_is_marked_legacy_fallback` |
| 5 | manual_override 无原因/空白原因被拒绝；非覆盖路径禁带原因 | `test_manual_override_requires_non_empty_reason` |
| 6 | budget_overrun_evidence 默认为空 | round-trip 与 legacy 测试中断言 `is None` |
| 7 | 创建事件只含摘要、不含大 JSON | `test_created_event_carries_summary_not_full_json` |
| 8 | 无 sizing 单独给 execution_budget 被拒绝 | `test_execution_budget_without_sizing_is_rejected` |

## Test Evidence

```text
$ .venv/bin/python -m pytest tests/test_budget_policy.py -q
..............                                                           [100%]
14 passed

$ git diff --check
(no output — clean)
```

## Files Touched (allowed scope only)

- `backend/plow_whip_web/domain/model.py`
- `backend/plow_whip_web/store/task_repository.py`
- `backend/plow_whip_web/store/migrations/0016_task_sizing_budget.sql` (new)
- `tests/test_budget_policy.py`
- `docs/SPRINT_10_SIZING_PERSISTENCE_RESULT.md`

SPRINT10_SIZING_PERSISTENCE_COMPLETE
