# Sprint 11 P0 Correction Verification Result

## 验证结论

- **COMPLETE**：本轮规定的确定性后端、迁移、Host Bridge race、UI 和 diff 质量门全部通过，关键机制代码证据成立。
- 该结论只表示当前工作树通过 P0 correction 的源码级独立验收；不表示全部产品能力已实现，也不表示当前 Docker 运行实例已部署此工作树。
- Task id：`29b64cf4-188c-4222-8aa3-91588823f5e9`
- Worker id：`7670722c-ebf3-49c2-85d4-effe30ad647f`
- 验证期间 HEAD 始终为 `7c18fdf`；未 reset、checkout、回滚、提交或推送。原脏树被保留，本 Worker 未修改实现。

## 已实现

### 编排、Provider 与完成合同

- `runtime/orchestration.py:41-83,197-203`：默认计划只根据结构化 sizing flags 选择角色，不按 title/objective 关键词路由；结构化计划必须显式声明角色。
- `runtime/orchestration.py:14-16,31-38,174-194` 与 `store/goal_repository.py:265-285`：`model_pm_implemented=false`、`model_invoked=false` 被诚实返回和持久化，未伪称模型 PM。
- `store/project_repository.py:34-47` 与 `api/app.py:501-512`：已有 `project+role` Worker 绑定优先；未绑定角色必须显式选择 Provider；创建前按唯一 Provider 排序并逐一执行 live readiness probe。
- `store/goal_repository.py:88-288`：Provider probe 完成后，goal、coordination parent、全部 child 和 `goal.created` event 在单个 immediate transaction 中创建；任一 child sizing 或写入失败不会留下部分 goal/task。
- `store/goal_repository.py:196-256,615-648`：每个 child 独立生成 sizing、`execution_budget`、hard deadline、`max_attempts`、token cap 和 verification/acceptance surface。
- `store/goal_repository.py:308-441`：依赖仅在前置 child 为 `completed` 时解锁；parent 只有在全部 implementation/verification child 完成且存在 verification child 时才 `completed`。
- `runtime/scheduler.py:55-120`：tick 前后调用幂等 `goals.advance()`；派发使用 fencing token + task id 作为 idempotency key。真实 HTTP generic-command goal 回归及完成后重复 tick 均通过。

### 预算、FaultPolicy、Journal 与 SQLite

- `store/task_repository.py:99-101,260-277,448-459,1025-1085`：estimated task 将 `execution_budget.max_attempts` 镜像到 `tasks.max_attempts`，读取、claim、retry 和 terminal decision 都以 execution budget 为权威；legacy task 才回退到列值。
- `migrations/0018_p0_correction.sql:17-22`：旧 estimated row 的 `tasks.max_attempts` 从 `execution_budget_json.max_attempts` 修复。
- `runtime/fault_policy.py:18-24,46-64,86-105`：只有锚定的 transport 签名进入 defer；`socket hang up` 不按普通 command failure 结算。内部 tool abort 只有在无进程 exit status 且输出不足时才算 no-progress。
- `runtime/task_service.py:260-309`：transient defer 保留 external session；同一 session generation 的内部 tool abort 连续达到阈值后才 rotate。
- `runtime/journal.py:34-42` 与 `runtime/task_service.py:405-428`：rotation 阈值只计算 `events.current.jsonl`；rotate 前归档当前 hot generation，历史 archive 不重复触发。
- `store/host_job_repository.py:204-235` 与 `store/task_repository.py:1101-1113`：SQLite 只保存状态、refs、segments、hash、bytes、offset、Token 和错误分类，不保存 stdout/stderr/prompt 正文。
- `migrations/0019_backend_correction.sql:1-32`：一次性 scrub 旧 `host_jobs`/`task_runs` 顶层及 nested execution 正文；回归确认 refs、hash、bytes、offset 保留。
- `store/migrations/0003_workforce.sql:35-43` 与 `store/goal_repository.py:541-549`：真实 schema 列名为 `worker_session_archives.archived_at`，查询也使用该列。

### UI

- `web/src/liveRefresh.ts:13-56`：EventSource 可用时只创建一个 SSE；不可用或报错时只启动一个 30 秒 poller；并发 refresh 被抑制；teardown 同时 close SSE 和 clear interval。
- `web/src/App.tsx:147-156,403-431`：选择 task 时清除 goal，选择 goal 时清除 task；Goal 子项展示角色/Provider、依赖、阻塞、session generation、rotation reason、sizing、budget、attempt、verification 和输出元数据。
- `web/src/App.tsx:412-431`：页面明确显示模型 PM 尚未实现，只展示 output ref/segments/bytes/offset，不绑定 stdout/stderr/prompt 正文。
- `web/src/App.tsx:443-450,565-570`：Provider 分层显示 installed/CLI probe、session resume readiness、recent execution health；单次 probe 成功不会掩盖最近执行不健康。只显示 credential 环境变量引用和 slot 数，不显示 key 值。
- `web/src/App.tsx:459-460`：Token 页面明确标记当前只统计已知 input/output，cache-read 未计入，不能视为完整成本。

## 部分实现

- mixed-provider 的 `project+role` 绑定、逐唯一 Provider probe 和失败无 goal/task 写入已由 API/Repository 回归验证；本轮没有执行真实 Codex+Cursor 混合 Provider goal E2E，因此不能宣称真实外部 CLI 全链路已验证。
- deterministic PM 模板可以按 sizing flags 生成最多 7 个有序 child，并强制最后一个为 verification；它不是模型 PM 自动拆分。
- 新角色 catalog、Goal UI 和 0017/0018/0019 已存在于当前工作树并通过源码级验收，但尚未进入当前运行容器。
- Provider 页面能如实展示 recent execution health；Provider capacity 目前仍没有独立 failure class，旧运行时现场仍记录为 `command_failed`。

## 未实现

- 模型 PM 自动理解目标并生成计划：未实现，合同值保持 `model_pm_implemented=false`。
- 真实 mixed-provider goal E2E：本轮未执行，不得声称通过。
- cache-read Token 采集与页面计量：未实现；页面明确显示“未计入/不可见”。
- Provider capacity 的专用分类、defer/retry 策略：未实现；旧运行时把 capacity 归为 `command_failed`。
- 当前 Docker 容器部署：未执行。运行容器 `/health` 仍报告 16 migrations，项目 API 仍只有 `coordination/devops_sre/fullstack/verification/web3` 五个 legacy 角色。

## 任务表现、人工干预与 Token/输出轮转事实

- Coordination/Cursor：受限运行库可确认 coordination Worker generation 2 因 `provider_tool_environment_unrecoverable_after_repeated_aborted_calls` 被归档，generation 3 Host job 以 returncode 0 完成并记录 145,075 input + 39,096 output Token。第一项 coordination task 后续被取消，第三代 task 因缺结果文件验证而 terminal_failed。受“不读取完整旧 Host 输出”边界限制，无法把“两个独立 generation 都因 Aborted 失效”逐代复原，因此不扩大该历史声明。
- Backend 短切片：task `1fa48dcb-d2e2-4a71-964d-4506c5a822d4` 的 Host job returncode 0；最终 `11,996,969` Token 超过 `1,200,000` cap，被 `budget_exceeded` 正确结算为 `terminal_failed`。这是预算熔断通过，不是后端测试失败。
- UI 短切片：task `89e15d34-9f23-4b33-ace1-7e6d75cb9250` 的受限输出证据显示 19/19、typecheck、lint、build 均通过，随后出现 `Selected model is at capacity`；Host/旧运行时持久化为 returncode 1、`command_failed`，`max_attempts=1` 后 terminal_failed，且未生成 UI 结果文档。
- UI Host 输出未整份读取。其 metadata 为 output ref `2fc4b5e2-cb1a-4122-9307-3c74902468ea/`、471,148 bytes，并已拆成 3 个输出 segment 文件；SQLite 只保存引用与统计。
- 当前 verification SessionJournal hot generation 为 4,315 bytes，低于 `rotation_max_bytes=262,144`，未达到 rotation 阈值；未读取 journal 正文。
- 运行态可见的人工介入包括先前 `operator_rebind`、coordination task 取消/换代，以及本轮独立 verification；本 Worker 没有修代码或替开发任务补实现。

## 本轮完整命令结果

| 命令 | 结果 |
| --- | --- |
| `.venv/bin/python -m pytest -q` | exit 0；244 passed |
| 连续 10 次 `.venv/bin/python -m pytest tests/test_host_job_continuity.py::test_bridge_restart_identifies_orphan_without_duplicate_and_can_cancel -q` | 10/10 passed |
| fresh `Database.migrate()` | 19 migrations；最后为 `0019_backend_correction.sql` |
| 同一 fresh DB 第二次 `Database.migrate()` | `[]`，无重复迁移 |
| fresh schema inspection | 0017/0018/0019 均记录；`worker_session_archives.archived_at` 为 `TEXT`；Goal/task 新列齐全 |
| `.venv/bin/python -m pytest tests/test_database.py -q` | exit 0；2 passed，覆盖旧数据 scrub 与 metadata 保留 |
| `pnpm test` | exit 0；1 file、19/19 tests passed |
| `pnpm typecheck` | exit 0 |
| `pnpm lint` | exit 0 |
| `pnpm build` | exit 0；Vite production build 完成 |
| `git diff --check` | exit 0；无输出 |

环境说明：预备执行 `corepack pnpm test` 时，Corepack 因沙箱禁止创建 `~/.node/corepack` 返回 EPERM，测试脚本未启动；随后使用已安装的 `pnpm 11.9.0` 运行相同 package script，实际 UI 质量门全部通过。

## 部署前剩余项

- 构建并部署包含当前工作树与 0017/0018/0019 的新镜像，再在部署态重验 migration count、八角色 catalog、Goal UI 和 SSE。
- 对生产数据备份后执行迁移，确认旧 body scrub 的 refs/hash/bytes/offset 保留，并抽查 `worker_session_archives.archived_at` 查询。
- 执行真实 Codex+Cursor mixed-provider goal E2E，包括 project+role session 绑定、Provider probe、child budget、verification parent completion 和重复 scheduler tick。
- 为 Provider capacity 增加准确分类前，不得把旧运行态 `command_failed` 解释为实现或测试失败。
- cache-read Token 接入账本前，成本页面只能作为已知 input/output 的下界。

SPRINT11_P0_CORRECTION_COMPLETE
