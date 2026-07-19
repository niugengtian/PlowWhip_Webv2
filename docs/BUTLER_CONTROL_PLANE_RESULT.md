# Butler Control Plane 实施结果

日期：2026-07-19
Task：`fc82ff2f-d0e5-4b17-b154-5f6aaf1f4df3`
交接来源 Task：`ad603b85-e44d-415b-b782-cb0ddb5be1e2`
TaskSpec revision：1
源码边界：`/Users/niugengtian/work/plow-whip-web-v2/github-main-20260719`

本报告只陈述当前工作区代码、SQLite 迁移和确定性验证事实。未部署或修改 8742，未使用网络，未调用付费 Provider，未 push。`queued`、wake、heartbeat、Host Job accepted 和模型文本均未作为完成证据。

## 1. 实现事实

### 1.1 统一 Butler intake

- `POST /api/butler/intakes` 接受 `structured` 与 `natural_language`，均写入 `butler_intakes`、`butler_questions` 和 `butler_events`。
- 兼容的 `POST /api/goals` 不再直接创建 Goal，而是先创建结构化 intake；手工 `POST /api/tasks` 仅保留为诊断命令，不是平行目标状态机。
- 确定性 size floor 不能被模型降级。大型目标同一时刻最多一个未回答问题；`confidence < 95` 不能派发；达到 95% 后仍须主人确认当前 proposal hash。
- 中小目标从请求、项目角色绑定和已就绪 Provider 中自动选择并生成有序 Task。Goal/Task plan 先通过稳定幂等键提交；随后 intake 的 `dispatched` 状态与全部初始 `worker.wake_requested` outbox 在同一 SQLite 事务提交。
- Web 目标抽屉已改为 intake/单问题/方案确认界面，不再直接调用 Goal 创建状态机。
- Butler 页提供持久化 help inbox；Task/Goal 详情读取同一 aggregate control-plane 读模型，展示 canonical revision、明确下一动作、Task 级 Session、最近 20 条 reducer lineage 与删除资格。该读模型不写状态，不构成平行状态机。

### 1.2 Task 级物理 Session

- 逻辑 Worker 仍由 `project + role` 表示；物理会话迁入 `provider_sessions`，唯一身份为 `project + role + task + session_generation`。
- claim 只查当前 Task 的 binding。同 Task retry 可复用该 generation；不同 Task 创建新的 binding，Worker 行上的旧 external session 不会跨 Task 续接。
- `workers.external_session_id` 仅保留为空的 schema 兼容列；运行时写入、Host Job 续接、Goal UI 和累计用量归因只读取 `provider_sessions`，不再镜像第二份 physical session 真相。
- rotate/rebind 会把关联的 bound/idle/terminating session 归档；Host Job 的 generation 与 session 回写均以 Task binding 为准。
- Context 继续由 global/project/task Convention、Task 状态和 bounded handoff 编译；journal 文件轮转不再被当作 Provider session 轮转。

### 1.3 单一版本化状态/证据协议

- 新增 `aggregate_transitions` 和 `AggregateReducer`。Task/Goal 状态转换在同一业务事务内追加 revision、actor、reason、previous/new state、previous/new evidence hash。
- Task create/claim/verify/finish/control/recovery、Goal create/unblock/settle/delete 均写入这一条 transition lineage；不存在第二个终态 ledger。
- 授权 evidence rewrite 只能通过 reducer API，要求 expected revision、actor、reason 和 idempotency key；相同命令重放返回原 transition，旧 revision或同 key 不同 evidence/actor/reason 返回 conflict。
- 只有确定性 verification 可产生 completed；usage、模型文本、wake、heartbeat 或队列状态不能覆盖证据。

### 1.4 Token observe-only 与调用对账

- `model_calls` 是唯一存储真相；旧 `token_usage` 仅为只读兼容 view；`token_reservations` 在 0021 中删除。
- 每个模型调用可保存 Goal/project/role/task/attempt/episode/worker/Host Job、Provider、physical session、generation、raw usage、snapshot kind 和 normalized usage。
- cumulative snapshot 按持久化 settlement sequence 连接同一 physical session 的 previous call；input/cached/output 分别计算单调 delta，单个计数器 reset 时只从该计数器当前值重新计量，normalized cached 继续限制为 normalized input 的子集。
- Host Job 在 prepare 时固化 `session_generation`；结算优先使用该 Job 的 external session/generation，即使当前 Task binding 已换代，也不会误记到新 Session。
- 即使 Provider 返回 0 Token，已发生的 Host 调用仍结算为可对账的 `model_calls` 行；Task usage 同样只增加 normalized delta。调用记录带 project/role/task/attempt/worker/Host Job/physical session 归因，未建立的 episode 保持空值而不伪造。
- Generic Command 是确定性本地执行，不再写入伪造的零 Token ModelCall；真实 API E2E 对此有反向断言。
- 调用记录直接保存 Goal/Task/Worker/Provider/Session attribution 与稳定 Goal/Task hash；Task/Goal 删除后清空可识别 id、保留 hash。Convention refinement 也进入同一 ledger，以 project-attributed one-shot call 结算，不再成为账外模型调用。
- `cached_input_tokens <= input_tokens`，总量固定为 `input + output`，不会重复加 cached。
- BudgetManager 已缩为 ModelCallLedger 兼容别名；旧 ensure/release/host-reservation API 和 `BudgetExceededError`/`budget_exceeded` fault 分支已删除。Token 不再拒绝 claim/dispatch、不触发 cancel/rotate，也不阻止 verification 终态。历史 sizing/token budget 字段仅作为估算兼容数据。

### 1.5 Help、reply、升级与安全中断

- `worker_help_requests`、`worker_help_replies` 持久化 category、severity、checkpoint、revision、sender 与 bounded same-Task context。
- `/api/butler/help`、reply API 和 outbox 事件覆盖 help requested、Butler reply、owner escalation 和 owner resolution。
- `GET /api/butler/help` 支持 project/goal/task/status 过滤且默认最多返回最近 20 条；Web 可直接完成 Butler 回复、升级给主人和主人解决，不依赖人工巡检日志。
- 事件协议显式区分 `worker.help_requested`、`butler.help_replied`、`owner.escalation_requested` 和 `owner.escalation_resolved`。
- 每个 Task 最多一个 `owner_escalated` 请求；checkpoint/reply context 由服务端写入权威 task id，跨 Task identity 输入返回 conflict。
- intake interrupt 先持久化 revisioned interrupted 事件，再给 Goal 下所有非终态 Task 发 cancel；API 返回每个 Task 的停止结果。Host Job 是否真正停止仍以 reconciliation 为准。
- Context、checkpoint、handoff、observation、journal rotation、same-failure/no-progress 阈值由同一 continuity policy 解析；Task Convention > Project Convention > global Settings。Context API 返回有效值、来源与警告；超限 help context 在持久化前拒绝，无法保留强制边界的 Context 不派发。

### 1.6 Task/Goal 删除

- `DeletionRepository` 统一 Task 与 Goal 删除语义，DELETE API 均要求 expected revision、reason 和 idempotency key。
- 活跃 Task 或未消费 Host Job 只进入 `stopping`，持久化 cancel control、session `terminating` 和 tombstone；同一 Goal 尚未派发的 ready/paused/needs-human 子 Task 在同一事务改为 `cancelled`，不会继续被调度。
- reconciliation 完成后在一个事务中删除控制面 Task/Goal/attempt/run/lease/session/help/outbox 依赖；Goal 明确 cascade 所有子 Task。
- 重复 DELETE 返回同一 tombstone；等待 Host reconciliation 期间不会反复推进 Goal revision；并发 stale revision 返回 conflict。
- ModelCall 清空 Goal/task/project/role/worker/Host Job 外键并保留 Goal/Task hash；audit payload 和 reducer lineage 使用哈希 identity；tombstone 保留 usage 数与 artifact references。
- 删除代码不调用文件删除，产物文件保持原位。Web Task/Goal 详情均提供带明确提示的删除操作。
- `GET /api/aggregates/{task|goal}/{id}/control-plane` 在删除前返回 `deletable`、`stop_required` 或 `stopping`、预期 revision、待停止 Task/Host Job、Goal cascade 数和下一动作。

## 2. 删除、合并或替换的机制

1. 删除运行时 Token admission、global reservation、settlement hard-cap terminal gate 和 budget-driven session rotation。
2. 删除 `token_reservations` 存储；把 `token_usage` 表迁入 `model_calls`，只留只读 view，消除双账本。
3. 把外部 Goal 创建兼容路由收敛到 Butler intake；Web 不再维护另一条 Goal 创建流。
4. 把物理 session 从 project-role Worker 行分离为 Task-bound binding，消除跨 Task resume。
5. 把 Task/Goal 终态与 evidence rewrite 的审计收敛到一个 versioned transition protocol。
6. 把 Task delete、Goal delete、active stop、cascade、usage/audit retention 合并为一个 deletion repository，而不是各自增加 permit 分支。
7. 删除旧 budget-policy 测试和旧 context-budget rotation 补丁测试；用 observe-only usage、Task session identity、bounded Context/journal 和删除/reducer测试替换。
8. 发布验证不再硬编码 migration count `20`，只验证 health/WAL/非空 migration ledger，避免每个迁移再改一处分支。
9. 删除 `ModelCallLedger` 内未被引用的第二个 `BudgetManager` 别名；兼容入口只保留在 `runtime/budget.py`。
10. Generic Command 不再进入 ModelCall 账本；recovery 直接状态写入已并入同一 reducer lineage。

没有新增第三方依赖或外部分布式工作流；并发、CAS、幂等和 cascade 继续使用 SQLite transaction、unique index 与 foreign key。

### 2.1 实现矩阵

| 验收面 | 当前实现 | 确定性证据 |
|---|---|---|
| 统一 intake / 大目标确认 | `ButlerRepository` 统一双输入、单 open question、95% 与 proposal hash gate | `test_structured_and_natural_inputs_share_one_intake_and_auto_dispatch`、`test_large_intake_has_one_question_and_needs_95_percent_owner_confirm` |
| 自动拆分 / wake / Session identity | `ButlerService` + `GoalRepository` + Task-bound `provider_sessions` | Butler Generic API E2E、`test_provider_session_identity_includes_task_and_never_cross_resumes` |
| help / reply / escalation / interrupt | 持久表、API、outbox 与 Web inbox | `tests/test_butler_control_plane.py` 相关 help、interrupt 测试 |
| observe-only Token | 单一 `model_calls` 表、累计 snapshot delta、无预算 Gate | `test_large_usage_never_blocks_verified_terminal_state`、累计/reset/Host attribution 测试 |
| 单一 reducer / evidence rewrite | Task/Goal/recovery transition lineage 与 CAS rewrite | `tests/test_unified_domain_reducer.py`、recovery lineage 断言 |
| Task/Goal 删除 | 两阶段停止、Goal cascade、匿名 usage/audit、保留产物 | 删除幂等/混合 active+ready/Goal-only ModelCall 测试 |
| 迁移 | 0020→0022 upgrade、fresh 22、第二次空集 | 两个 migration 专项测试与 fresh probe |
| 前后端交付 | Backend 261 tests；Web 21 tests + typecheck/lint/build | 本报告第 3 节列出的命令与退出码 |

## 3. 验证命令与结果

### 3.1 后端完整测试

```bash
PYTHONPATH=backend:. pytest -q
```

结果：进度到 `100%`，退出码 0，无失败。独立 collection 结果为 `261 tests collected`。

覆盖的关键事实包括：Butler 双输入、一个 open question、95%+owner confirm、自动 Provider/拆 Task/wake、Task-bound session、旧 Host Job 换代后仍归因原 Session、Goal/Task/Worker/Provider/Session 对账、账内 Convention refinement、observe-only 大额 usage、独立 counter reset delta、Generic Command 非模型调用、recovery reducer lineage、continuity 来源/超限拒绝、evidence rewrite lineage、help/reply/escalation、interrupt、Task/Goal 删除和并发/幂等。

### 3.2 迁移 fresh / upgrade / idempotent

```bash
PYTHONPATH=backend:. pytest -q \
  tests/test_database.py::test_migrations_are_idempotent \
  tests/test_database.py::test_0021_and_0022_upgrade_0020_database_to_unified_control_plane
```

结果：通过。额外 fresh probe：`22` 个迁移，最后为 `0022_butler_intake_help.sql`；同库第二次 migrate 返回 `[]`；`PRAGMA integrity_check = ok`。

### 3.3 Provider fixture 与 API E2E

```bash
PYTHONPATH=backend:. pytest -q \
  tests/test_host_job_continuity.py::test_host_job_persists_pid_session_and_is_idempotent \
  tests/test_butler_control_plane.py::test_butler_api_dispatch_to_real_generic_provider_and_verification_e2e
```

结果：通过。

- Host fixture 实际启动本地 fake-codex executable，验证 PID、早期 session id、usage、完成状态和重复 start 幂等；不是仅 monkeypatch 返回模型文本。
- API E2E 从 Butler intake 创建 Goal/Task，真实启动 Generic Command 子进程写入 `butler-e2e.txt`，再经 Scheduler 驱动独立 verification；断言 Goal/Task completed、文件内容、evidence hash，以及 session row/control-plane 投影中的 `project + role + task` identity。

受 `network:none` 限制，本次没有调用真实付费 Codex/Cursor/DeepSeek 服务；上述结果证明本地 Provider/Host contract fixture 与控制面闭环，不伪称厂商线上执行成功。

### 3.4 前端

当前 workspace 的 `web/node_modules` 指向只读依赖目录，因此使用 Vite/Vitest 原生 runner config loader，未改依赖、未联网安装：

```bash
cd web
npm test -- --configLoader runner
npm run typecheck
npm run lint
npm run build -- --configLoader runner
```

结果：`1` 个 Test File、`21` tests passed；TypeScript 通过；ESLint 通过；Vite production build 通过（4559 modules，JS 336.43 kB / gzip 96.55 kB）。

### 3.5 静态交付检查

```bash
git diff --check
```

结果：退出码 0，无 whitespace error。

## 4. 已知剩余风险与边界

1. **未做线上厂商执行**：network:none 且禁止改动 8742；真实 Codex/Cursor/DeepSeek 认证、累计 usage 形态和 container-to-Bridge 路径仍需在获准部署环境验证。
2. **未部署迁移**：只证明 fresh DB 与 0020→0022 本地升级；当前 8742 数据库未触碰。部署前必须备份并在单一 release writer 中验证实际数据量与 migration health。
3. **删除是两阶段命令**：活跃 Host Job 的首次 DELETE 合法返回 202/stopping；只有 Host cancel/reconciliation 后的后续调用才完成物理控制行删除。不能把 cancel requested 当 stopped。
4. **兼容字段仍存在**：Task/API schema 中的 `token_budget`、sizing hard-cap key 是历史估算兼容字段，UI 已标为 observe-only estimate，不再是控制 Gate。彻底改名需要另一个客户端/schema 兼容迁移，但当前运行时无预算拒绝路径。
5. **诊断 Task API 保留**：`POST /api/tasks` 可用于单 Task 故障诊断；它不创建 Goal，因此不构成第二个目标 intake，但仍应由部署层权限限制。
6. **删除后 lineage 只保留匿名 identity**：这是隐私要求的结果；按原始 Task/Goal id 查询历史 transition 会转由 tombstone/audit hash 对账，而不是返回可识别 lineage。
7. **continuity override 是显式机器声明**：Project/Task 的数值覆盖必须使用单行 `Continuity-Limits` JSON；普通 Convention prose 仍作为上下文规则，不会被猜测成配置。当前 Web 未提供专门的可视化 override 编辑器，可通过 Convention API/编辑器维护并在 Context API 核对来源。
8. **intake dispatch 是可重试的两事务边界**：Goal/Task plan 的幂等创建先提交，随后 intake `dispatched` 与 wake outbox 原子提交。两事务之间进程退出会留下 `dispatching` intake 与已创建 Goal；使用相同 intake/goal 幂等键重试可收敛，但当前没有单独的后台 intake recovery scanner。

## 5. 结论

当前工作区已实现本 TaskSpec revision 1 要求的统一 Butler intake、Task-scoped physical session、observe-only ModelCall ledger、版本化状态/证据 lineage、Web help/owner escalation、canonical 下一动作、安全中断和两阶段 Task/Goal 删除。全套后端、前端、迁移、真实本地 executable fixture 与 API E2E 均有退出码 0 的确定性证据。

由于约束明确禁止网络、部署和修改 8742，本报告不把这些本地证据扩大为线上 Provider 或部署验收。交付状态是“源码与本地确定性验收通过；部署/真实厂商线上执行未执行”。
