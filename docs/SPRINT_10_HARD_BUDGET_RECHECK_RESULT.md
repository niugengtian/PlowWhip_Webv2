# Sprint 10 Hard Budget Recheck Result

## 结论

- 唯一结论：**PASS**
- 复验范围仅覆盖 V2 最终预部署报告的 hard-budget 唯一阻断项及回归。
- Task id：`4d842cfb-8135-4614-baab-424c34c44852`
- Worker id：`7670722c-ebf3-49c2-85d4-effe30ad647f`

## 命令证据

| 命令 | 退出码 | 结果 |
| --- | ---: | --- |
| `.venv/bin/python -m pytest tests/test_budget_policy.py -q` | 0 | 30 passed |
| `.venv/bin/python -m pytest -q` | 0 | 231 passed |
| `git diff --check` | 0 | 通过，无输出 |

## 静态合同证据

- `backend/plow_whip_web/store/task_repository.py:415-428` 将历史累计 Token 与本次输入/输出相加；estimated 任务读取 `execution_budget.total_token_hard_cap`，legacy fallback 读取持久化的 `task.token_budget`；只有 `actual_tokens > token_hard_cap` 才算 over cap，因此等值 cap 被允许。
- `backend/plow_whip_web/store/task_repository.py:428-454` 对 over cap 固定设置 `budget_exceeded`，并由 `over_cap` 直接选择 `TaskStatus.TERMINAL_FAILED`，不再以 evidence 有效性作为完成判据。
- `backend/plow_whip_web/store/task_repository.py:463-483` 持久化实际 Token、验证 evidence hash 和错误原因；仅当 over cap 且调用方提供 evidence 时将其写入 `budget_overrun_evidence_json`，用途为审计，不改变终态。
- `backend/plow_whip_web/store/task_repository.py:490-545` 将 over-cap attempt/run 结算为 `failed`，保留 execution、verification 和 budget 结果，并发出 `task.terminal_failed`。
- `backend/plow_whip_web/store/task_repository.py:402-407` 在任何新结算前按 finish idempotency key 返回既有任务记录，避免重复累计或翻转终态。
- `tests/test_budget_policy.py:857-870` 验证实际 Token 等于 hard cap 时可 `completed`。
- `tests/test_budget_policy.py:873-945` 覆盖无 evidence、格式有效 evidence 和格式错误 evidence；三种 over-cap 输入均为 `terminal_failed`，且所提供 evidence 仅作审计持久化。
- `tests/test_budget_policy.py:963-995` 验证重复 finish 不增加 Token usage、run 或 event，也不改变 revision 和 `terminal_failed` 状态。
- 静态搜索确认旧 `valid_overrun_evidence` helper 与 `test_finish_over_cap_with_valid_evidence_completes_and_persists_it` 测试均已删除。

## B4 现场证据与剩余部署风险

- 当前 B4 任务由旧 `legacy_fallback` 创建链路产生，并因其持久化 `task.token_budget` 被结算为 `terminal_failed`。这是部署前既有实例仍存在预算双真相的现场证据，不是本轮测试失败，也不否定当前源码 hard-budget 合同。
- 本轮只验证当前工作树源码与本地测试，没有部署、重建或检查 Docker 镜像；**不宣称 Docker 已更新**。
- 部署时仍需确认运行实例/镜像包含当前代码，并对旧 legacy 任务与新 `ExecutionBudget` 任务的预算权威来源作明确迁移或隔离；在完成部署态验证前，B4 所示的旧实例风险仍然存在。

SPRINT10_HARD_BUDGET_RECHECK_PASS
