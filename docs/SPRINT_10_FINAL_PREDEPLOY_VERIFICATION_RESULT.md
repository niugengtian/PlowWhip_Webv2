# Sprint 10 最终预部署独立验收

- 验收角色：独立 QA Worker
- Task id：`0f9b759a-8353-4146-8b4e-58d7a7552727`
- Worker id：`7670722c-ebf3-49c2-85d4-effe30ad647f`
- 验收时间：2026-07-17 20:41:44–20:48:15（Asia/Shanghai）
- 工作树：`/Users/niugengtian/work/plow-whip-web-v2`
- 分支：`codex/sprint-9-execution-continuity`
- HEAD：`2f5d8aeb38b3d3a1e9b8b007d3aa1420964b2d79`
- 唯一结论：**CHANGES_REQUIRED**

## 结论依据

全部必选测试、静态检查和构建命令均以退出码 0 完成，但“预算超限不得
`completed`”这一必选合同不成立。当前实现允许实际 Token 超过
`execution_budget.total_token_hard_cap` 后，凭 `budget_overrun_evidence`
进入 `completed`；现有测试还明确把该行为作为成功路径。因此不能给出
最终预部署 PASS。

这不是由旧报告推断出的结论。本次重新运行了当前工作树的测试和构建，并直接
审阅当前生产路径与对应测试。

## 命令证据

| # | 命令 | 退出码 | 本轮事实 |
|---|---|---:|---|
| 1 | `.venv/bin/python -m pytest -q` | 0 | 全量后端成功；进度到 100%。本仓库 quiet 配置不打印汇总，随后用 collect-only 清单确认本轮为 236 项。 |
| 2 | `.venv/bin/python -m pytest --collect-only -q` | 0 | 13 个测试文件，共 236 项：2+35+7+1+12+21+10+11+111+11+4+7+4。 |
| 3 | `pnpm test`（cwd=`web`） | 0 | 1 个 test file，12 tests passed。 |
| 4 | `pnpm run typecheck`（cwd=`web`） | 0 | `tsc -b --pretty false`，无错误。 |
| 5 | `pnpm run lint`（cwd=`web`） | 0 | `eslint .`，无错误。 |
| 6 | `pnpm run build`（cwd=`web`） | 0 | Vite 8.1.4；4558 modules transformed；生成 0.75 kB HTML、17.82 kB CSS、306.29 kB JS。 |
| 7 | `git diff --check` | 0 | 首轮检查无输出。 |
| 8 | `find backend/plow_whip_web/store/migrations -maxdepth 1 -type f -name '*.sql' -print \| sort`，随后 `.venv/bin/python -m pytest tests/test_app.py tests/test_database.py -q` | 0 | 动态发现 `0001`–`0016` 共 16 个迁移；fresh/idempotent/health 合同 3 项成功。 |
| 9 | `.venv/bin/python -m pytest tests/test_budget_policy.py tests/test_fault_policy_runtime.py tests/test_host_job_continuity.py::test_estimated_host_dispatch_uses_execution_budget_deadline_and_reservation tests/test_provider_pool.py::test_execution_deadline_helpers_use_budget_or_legacy_command tests/test_provider_pool.py::test_host_bridge_client_http_timeout_allows_m_hard_deadline tests/test_provider_pool.py::test_provider_pool_passes_execution_budget_hard_deadline_to_host tests/test_tasks_api.py::test_verification_failure_cannot_complete tests/test_release_security.py::test_legacy_quality_profiles_use_one_deterministic_execute -q` | 0 | 55 个定向实例成功。 |
| 10 | `.venv/bin/python -m pytest tests/test_budget_policy.py::test_finish_over_cap_with_valid_evidence_completes_and_persists_it -q` | 0 | 1 项成功；测试名和断言直接证明“超 hard cap 仍 completed”的当前行为。 |
| 11 | `rg -n "fast\|balanced\|strict\|50[_,]?000\|600" web/src/App.tsx web/src/api.ts` | 1 | 预期的 no-match；生产任务 UI/API 无这些旧可选档位或固定值。 |
| 12 | `rg -n -i "fake.{0,24}plan\|plan.{0,24}fake\|strict.{0,24}(double\|twice\|second)\|same.{0,24}session.{0,24}(review\|reviewer)\|independent.{0,24}(review\|reviewer).{0,24}session" backend/plow_whip_web --glob '*.py'` | 1 | 预期的 no-match。 |
| 13 | `rg -n "record_quality_run\\(" backend/plow_whip_web --glob '*.py'` | 0 | 仅命中 `TaskRepository.record_quality_run` 定义，无生产调用者。 |
| 14 | `rg -n "quality_profile" backend/plow_whip_web/runtime backend/plow_whip_web/providers --glob '*.py'` | 0 | 仅命中 TaskService 的弃用兼容说明；运行路径不按 fast/balanced/strict 分支。 |
| 15 | `git diff --binary \| shasum -a 256`，以及 `git ls-files --others --exclude-standard -z \| xargs -0 shasum -a 256`（验收前后各一次） | 0 | tracked diff 哈希始终为 `d848d10bd648910eb136ae675ae36d3d236d23d86aaed4b2892ad48e3ab64184`；报告创建前全部既有 untracked 文件哈希逐项相同。 |
| 16 | `git status --short`，随后 `git diff --check`（报告创建后最终检查） | 0 | status 仅比初始基线多出本报告；diff check 无输出。tracked diff 哈希仍为 `d848d10b…`，其余既有 untracked 哈希均未变化。 |

补充说明：曾读取 `.pytest_cache/v/cache/nodeids`，得到 247；这是累积缓存，和本轮
collect-only 的 236 项不一致，故没有用该缓存数字作为通过数。

## 定向合同逐项判定

### 1. estimate API 不消耗模型 Token — PASS

- `backend/plow_whip_web/api/app.py:444-451`：API 只调用
  `estimate_task_sizing()`，没有 Provider/模型调用。
- `backend/plow_whip_web/runtime/sizing.py:121-166`：纯结构化规则计算，并固定返回
  `model_invoked=False`。
- `tests/test_budget_policy.py:212-245`：两次请求结果稳定，前后
  `token_usage` 行数相等。

### 2. XS–XL 均可达 — PASS

- `backend/plow_whip_web/runtime/sizing.py:44-100,253-262`：五档均有完整预算，
  分数阈值覆盖 XS/S/M/L/XL。
- `tests/test_budget_policy.py:107-195` 覆盖 XS、M、L、XL；
  `tests/test_budget_policy.py:684-711` 的 S 预览断言覆盖 S。

### 3. 独立复审缺编排时 needs_planning 且预算为空 — PASS

- `backend/plow_whip_web/runtime/sizing.py:132-135,174-197`：一旦要求独立复审，
  加入 `independent_review_orchestration` gate；所有 Token、deadline、attempt
  和 reservation 字段返回 `None`。
- `tests/test_budget_policy.py:81-104` 对全部空预算字段逐项断言。

### 4. create 使用最新 sizing_inputs 并持久化单一 ExecutionBudget — PASS

- `web/src/App.tsx:138-169`：只有最近一次预判仍对应当前 `sizingInputs` 才允许
  create；任一 sizing 输入变化会清空预判。
- `backend/plow_whip_web/api/app.py:492-544`：create 不信任旧预览，服务端用提交的
  `sizing_inputs` 重新计算；`needs_planning` 直接拒绝。
- `backend/plow_whip_web/api/app.py:661-682`：描述性 sizing 与唯一
  `execution_budget` 分离。人工覆盖时
  `token_budget == execution_budget.total_token_hard_cap`，原估值只作为审计字段。
- `tests/test_budget_policy.py:564-601,626-711,767-792` 覆盖预览一致性、伪造预算
  拒绝、单一执行 cap 和幂等不覆盖。
- 兼容风险：API 仍允许不带 `sizing_inputs` 的旧调用落为
  `legacy_fallback`；新前端不会走该路径。

### 5. Host 使用动态 deadline/lease/reservation — PASS

- `backend/plow_whip_web/store/task_repository.py:34-53`：estimated task 的 hard
  deadline 与 reservation 从 `execution_budget` 读取；lease 为 hard deadline
  加 60 秒安全余量（至少 300 秒）。
- `backend/plow_whip_web/store/task_repository.py:933-949`：claim 时把动态 lease
  同时写入 task lease 和 resource lock。
- `backend/plow_whip_web/providers/pool.py:93-109`：Host start 使用动态 hard
  deadline。
- `backend/plow_whip_web/runtime/task_service.py:79-91,167-170`：claim 前传入动态
  reservation，运行中按动态 lease 续租。
- 定向测试验证 M=1200 秒/150000 Token，并验证 L=2400、XL=4800 不再被
  600 秒固定值截断。

### 6. 预算超限不得 completed — FAIL

- `backend/plow_whip_web/store/task_repository.py:428-466`：
  `valid_overrun_evidence` 在超 cap 且验证成功时可为真；此时
  `budget_reason=None`，目标状态成为 `COMPLETED`。
- `tests/test_budget_policy.py:914-933` 明确构造
  `actual_tokens=101 > total_token_hard_cap=100`，随后断言
  `finished.status == completed`。
- 本轮单测
  `test_finish_over_cap_with_valid_evidence_completes_and_persists_it`
  退出码 0，说明这不是死代码或未执行分支。
- 该合同要求是“超限不得 completed”，没有给证据例外；当前实现不满足。

### 7. 普通验证失败自动进入 terminal_failed — PASS

- `backend/plow_whip_web/api/app.py:478-482`：有确定性 argv 的普通命令默认只有
  1 次 attempt。
- `backend/plow_whip_web/store/task_repository.py:457-466,547-555`：不能重试的普通
  验证失败进入 `TERMINAL_FAILED` 并发出 `task.terminal_failed`，不误入
  `needs_human`。
- `tests/test_tasks_api.py:152-176` 通过真实 create/drive 路径断言
  `terminal_failed`。
- 边界：可产生新模型结果的无 argv 任务仍按 attempt/fingerprint 策略重试；
  本项 PASS 限于验收所称“普通确定性验证失败”。

### 8. FaultPolicy 瞬时故障可续接且幂等 — PASS

- `backend/plow_whip_web/runtime/fault_policy.py:18-74`：只把已知传输签名分类为
  transient；普通 command failure 仍进入验证。
- `backend/plow_whip_web/runtime/task_service.py:149-257`：Host reconcile 生产路径
  实际调用 FaultPolicy。
- `backend/plow_whip_web/store/task_repository.py:620-759`：稳定 idempotency key，
  保留 external session，释放/结算 reservation，回到 ready 且不消耗 attempt。
- `tests/test_fault_policy_runtime.py:137-224,227-260`：验证 socket hang up 后同
  session 续接、重复 reconcile 不增加 revision/event/token usage，以及非零 Token
  仅结算一次。

## 反向扫描 — PASS

- 前端生产 `App.tsx`/`api.ts` 不含用户可选 fast/balanced/strict，也不含固定
  50000/600；create 固定提交 `deterministic`，预算和 timeout 展示来自 estimate。
- 后端仍在 API schema 中接受旧 quality 字符串，但 validator 一律规范化成
  `deterministic`；TaskService 只有一次确定性验证调用，没有 strict double-run。
- 未发现 fake plan 或 same-session independent review。要求独立复审时反而被
  planning gate 阻断。
- `record_quality_run` 仍有无调用的兼容方法定义；当前没有运行路径使用它。

## 迁移 — PASS

- runtime 与测试都用 `sorted(migration_dir.glob("*.sql"))` 动态发现清单，而不是
  固定数量。
- 本轮发现 `0001_initial.sql` 到 `0016_task_sizing_budget.sql` 共 16 个文件。
- fresh 临时 SQLite 上首次 migrate 返回完整动态清单，二次 migrate 返回空，
  health migration_count 等于清单长度；相关 3 项测试成功。

## 脏树与并发稳定性

- 验收开始即存在大量 tracked/untracked Sprint 10 脏改动；全部保留，没有
  reset、回滚、覆盖、提交或推送。
- 20:41:44、20:46:09 与报告创建后的 20:48:15 三次快照间，tracked diff 总
  哈希相同；除本报告外，每个既有 untracked 文件的 SHA-256 也相同；未发现
  并发变化。
- QA 唯一新增文件是
  `docs/SPRINT_10_FINAL_PREDEPLOY_VERIFICATION_RESULT.md`。

## 未覆盖风险

- 按任务边界只验证代码和测试，没有启动、更新或检查 Docker 当前运行实例；
  本报告不宣称任何容器正在运行最新工作树。
- Host/FaultPolicy 定向测试使用 Fake Host Bridge，没有执行真实 Codex/Cursor
  CLI 端到端；真实进程、网络和凭据行为仍是部署前运行环境风险。
- fresh migration 验证针对源码树中的 SQLite 临时库；没有验证已构建 wheel 或
  Docker 镜像内是否包含同一迁移清单。
- 旧 API 仍保留 `legacy_fallback`，因此绕过新前端直接调用 create 时可以没有
  sizing/ExecutionBudget；这是兼容面，不是新前端主路径。
- 最重要的阻断风险是已证实的 over-cap completion 例外；在消除该例外前不能
  满足本次最终门禁。

SPRINT10_FINAL_PREDEPLOY_CHANGES_REQUIRED
