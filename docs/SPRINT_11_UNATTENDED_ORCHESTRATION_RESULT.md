# Sprint 11 — Unattended Orchestration Result

## Summary

在 `7c18fdf` 之上落地统一无人值守编排：用户只提交一个目标，PM 0 Token 确定性拆分角色工作项，Scheduler 按有序依赖自动派发/续接/验证，父目标仅在实现项与独立 verification 项全部通过后完成。复用既有动态 sizing/预算/超时、Provider 就绪门禁、Host 输出文件轮转与 DeepSeek 配置，未重做这些闭环。

## Implemented

- `0017_goal_orchestration.sql`：`goals` 表 + tasks 的 `goal_id` / `parent_task_id` / `depends_on_json` / `work_item_kind` / `ordinal` / `blocked_reason` / `handoff_json`
- `runtime/orchestration.py`：确定性 PM 拆分（线性 1–7 里程碑，末项强制 verification）
- `store/goal_repository.py`：目标创建、依赖解锁、父目标结算（幂等）
- Scheduler tick：先 `goals.advance()`，再派发 ready 子项；派发后再 advance
- 同 `project+role` 稳定会话；journal 持久化字节达 `rotation_max_bytes` 时 `context_bytes_threshold` 升 generation
- Context 注入 Evidence Delta 与结构化 Role Handoff（路径/hash，不复制跨角色历史）
- `finish` 写入 SQLite 时剥离 `stdout`/`stderr`/`prompt`，大输出仍走文件/Journal
- API：`POST/GET /api/goals`
- UI：主按钮「提交目标」；「诊断任务」保留手工单任务入口
- 文档：README / ARCHITECTURE / RUNBOOK 已更新

## Not implemented / out of scope

- 通用复杂 DAG
- 按模型自报动态涨预算
- 无限延长 soft/hard deadline
- 更多 Provider
- 用模型做 PM 拆分（本期拆分为 0 Token 规则）

## Migration

- 新增：`backend/plow_whip_web/store/migrations/0017_goal_orchestration.sql`
- fresh migrate 包含 0017；重复 migrate 幂等（二次空列表）

## Command results

```text
.venv/bin/python -m pytest
→ 238 passed

cd web && corepack pnpm test
→ 14 passed

cd web && corepack pnpm run typecheck
→ exit 0

cd web && corepack pnpm run lint
→ exit 0

cd web && corepack pnpm run build
→ exit 0

git diff --check
→ exit 0

.venv/bin/python -m pytest tests/test_orchestration.py::test_goal_to_auto_advance_e2e_with_real_http
→ passed（真实 HTTP TestClient：目标→拆分→tick 自动推进→父目标 completed；未伪造模型响应，使用 generic-command）
```

## Acceptance mapping

| 要求 | 证据 |
| --- | --- |
| fresh migration / 幂等 | `test_fresh_and_idempotent_migration_adds_goals` |
| PM 拆分确定性 | `test_pm_split_is_deterministic_and_ends_with_verification` |
| 不同角色不共享 session | E2E + `test_roles_do_not_share_session_across_goal_items` |
| 同角色跨工作项续接 | 既有 UNIQUE(project,role) + workforce 测试仍绿 |
| 字节 rotation 依赖文件 | `SessionJournal.current_bytes` + `context_bytes_threshold` |
| SQLite 不含完整 stdout/stderr/prompt | finish 元数据剥离 + E2E 断言 |
| Provider 不就绪不创建/派发 | 复用既有 create/drive `require_ready` |
| 动态预算超时传递 | goal 子项持久化 `sizing`/`execution_budget` |
| Evidence Delta 续修 | 既有 `last_failure_delta` + handoff |
| 独立 verification 才完成父目标 | `GoalRepository.advance` |
| scheduler 重启/重复 tick 幂等 | E2E 完成后重复 tick |
| 前端任务流 | `submits a goal through the primary PM entry` 等 14 测 |

SPRINT11_UNATTENDED_ORCHESTRATION_COMPLETE
