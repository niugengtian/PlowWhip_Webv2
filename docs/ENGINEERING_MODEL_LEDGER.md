# PlowWhip Web 模型执行台账

> 自动生成的路由索引；禁止直接编辑。真源：`docs/engineering-ledger/`。

```yaml
project: PlowWhip Web control plane
project_key: plow-whip-web-v2
ledger_revision: 2026-07-21.5
scope: this project only
```

## LOAD

1. 重大改造前完整读取本文件。
2. 从 `ROUTE` 选择最具体领域，用 `context --domains` 生成 Task 专属包；不要读取 OPEN 全集。
3. 只有 Task 包中的条目才读取独立真源正文；现场另行核对 Git/DB/image/活动执行。
4. 超过 Context 上限时缩小领域、指定条目或拆 Task，禁止截断必需合同。
5. TaskSpec 保存入选 ID、entry/ledger revision、hash、领域和选择原因。

## CORE

| ID | rev | 执行摘要 |
| --- | ---: | --- |
| R-001 | 1 | 用户只提供自然语言指令；项目管家应自行理解、澄清、拆分、选角色/规则/Provider、绑定 Task Session、派发、监控、恢复、验收和交付。 |
| R-004 | 1 | 重试第一步应读取同一 Task 的结构化 checkpoint、已验证 artifact、Git/工作区增量、上次故障和最小待办，再做最小检测。 |
| R-007 | 1 | 管家必须根据目标自动分配安全、可读的项目相对产物路径，并在确认方案和任务详情中显示；用户不需要知道内部目录深度。 |
| R-013 | 1 | 全局 Convention 只定义台账的强制使用规则、字段模板、读取时机和最低验收要求，不保存各项目的具体需求、事故正文或处理历史。 |

## ACTIVE INDEX

- active incidents: `15`
- status counts: `MITIGATED_UNVERIFIED=5, REQUIREMENT_OPEN=1, USER_REPORTED=4, VERIFIED_OPEN=5`
- Task Context limits: `16 entries / 12000 summary chars`
- 具体问题只在路由后的 Task Context Pack 中展开。

## ROUTE

| domain | 说明 | 必读条目 |
| --- | --- | --- |
| butler | Butler, Planner, clarification, confirmation, roles and project isolation | R-001,R-002,R-003,R-005,R-012,R-013,I-002,I-005,I-006 |
| lifecycle | Task/Goal state, dependencies, scheduling, retry and concurrency | R-001,R-004,R-006,R-007,R-012,I-003,I-004,I-010,I-013,I-015 |
| provider | Provider, SessionBinding, Host Bridge, network and recovery | R-003,R-004,R-005,R-006,R-010,I-006,I-010,I-011,I-015 |
| evidence | EvidenceManifest, verification, artifacts and reports | R-007,R-012,I-003,I-004,I-005,I-015 |
| token | Token, Context, usage dashboard and refresh consistency | R-004,R-008,R-009,I-007,I-008,I-013 |
| project | Project registration, paths, deletion, security and ledger ownership | R-010,R-011,R-013,I-001,I-012 |
| ui | Primary frontend flows, scope, live state, sorting and observability | R-002,R-008,R-009,R-012,I-001,I-002,I-007,I-008,I-009,I-013,I-014,I-015 |
| release | Migration, release, deployment, restart and reproducible bootstrap | R-005,R-010,R-011,I-006,I-011,I-012 |
| ui.sorting | Terminal/candidate history ordering and stable timestamps | R-009,I-014 |
| ui.live | Live Task state, model output, refresh generations and unsettled usage | R-008,R-009,I-003,I-013,I-015 |
| ui.scope | Project/global scope switching and stale selection/data prevention | R-008,R-009,I-008,I-009 |
| ui.layout | Responsive layout, overflow, labels and Token number readability | R-009,I-007,I-009 |
| ui.butler | Global/project Butler entry points, chat and model failure recovery | R-002,R-009,I-002 |
| task.dependencies | DAG validation, evidence-qualified unlocking and revision invalidation | R-001,R-007,R-012,I-003,I-004,I-013 |
| task.retry | Checkpoint resume, Watchdog, attempts and idempotent replanning | R-004,R-006,I-003,I-010,I-015 |
| provider.network | Network zones, Provider probing, suspension, switching and recovery | R-005,R-006,I-006,I-011 |
| token.dashboard | Normalized usage, live settlement, project/day scope and presentation | R-008,R-009,I-007,I-008,I-013 |
| project.registration | Project identity, host/control paths, bootstrap and ledger ownership | R-010,R-011,R-013,I-001,I-012 |

## FORBIDDEN

- 不用 queued/accepted/heartbeat/模型声明/单独 exit 0 证明完成。
- 不把 CHANGES_REQUIRED、网络或 Provider 故障写成 PASS。
- 不通过重复 Task、特殊状态、no-op 改文件或人工改库推进流程。
- 不重放旧聊天、完整日志、完整 DOM 或跨项目 ledger。
- 不在活动 Host Job/模型调用期间重启、部署或迁移。
- 不把工作树、提交、远端、image 和运行数据库称为同一版本。

## SOURCE

- manifest revision: `2026-07-21.5`
- `scripts/engineering_ledger.py check` 验证真源和两个生成视图一致。
- `scripts/engineering_ledger.py context --domains <domain,...>` 生成 Task 最小上下文。
- 新事故写入 `incidents/open/`；关闭后移入 `incidents/archive/YYYY/`，模型视图不再加载正文。
