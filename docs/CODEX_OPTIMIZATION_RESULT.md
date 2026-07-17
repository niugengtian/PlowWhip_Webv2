# Codex P1 优化结果

日期：2026-07-17

## 工作树边界

开始时仓库已有未提交修改，涉及 Host Bridge 产物索引/打开接口、任务产物 UI、Provider 展示和相关文档测试。本次没有 reset、checkout、回滚、删除、提交或推送，也没有覆盖这些用户修改；新增优化沿现有生产路径叠加。

## 实际闭环

### 1. Host Provider Token 预算预留

- 新增迁移 `0014_token_reservations.sql`，按 `run_id` 保存 active/settled/released 预留、预留量和实际量。
- Host 模型任务必须有正的任务剩余 Token。任务 claim 在同一个 SQLite `BEGIN IMMEDIATE` 事务内读取当前系统设置、检查任务预算、汇总当日实际用量与 active 预留，并预留任务剩余全部额度；`token_budget=0` 在 claim/派发前拒绝。
- 并发 claim 由同一数据库写事务串行化，不能重复占用同一份全局日额度。`/api/usage` 增加 active `reserved_tokens`。
- 正常完成按 CLI 上报用量结算预留；取消或外部中断如有上报用量则入账并结算，无上报量则释放；claim 后、Host Job 创建前崩溃的 stale recovery 会释放预留。
- 外部中断上报的 Token 同时写入任务累计用量，后续重试只能预留剩余额度。

这是一条调度与账本层的安全预留门，不是 Provider 侧生成硬截止。Codex、Cursor、JSON Worker 当前没有统一、可验证的最大 Token 固定 argv；真实 CLI 若上报实际量超过预留，系统只能事后如实入账，不能撤回已经发生的费用。

### 2. 系统级最大在途并发

- `max_parallel_workers` 现在统计 `running`、`verifying`、`stopping` 任务与未消费 Host Job 的去重总量。
- Scheduler 使用 `max_parallel_workers - active` 计算本 Tick 的 `available_slots`，不会把每个 Tick 的新增上限误当作系统总上限。
- 事务性 claim 再次执行同一硬门，因此手工 drive 和并发调用也不能绕过 Scheduler。
- revision/CAS 检查保持不变。回归测试不再假设同秒创建任务的 UUID 顺序，而是对实际仍为 `ready` 的任务使用当前 revision 验证并发门。

### 3. Artifact evidence hash

- `file_exists` 和 `file_contains` 检查现在把实际文件的 SHA-256、字节数和纳秒修改时间写入 verification checks；这些字段进入 canonical evidence hash。
- 容器内 Generic Command 和 Host Bridge 验证都复用同一个 `VerificationEngine`，Host 文件仍只在允许的主机项目根内读取。
- artifact 内容或修改时间变化会改变 evidence hash。相同失败循环的 guard 使用排除修改时间、但保留内容 SHA/大小和执行结果的稳定 fingerprint，避免任务仅因重复写入同一失败文件而绕过防循环阈值。

### 4. Context 安全截断

- Boundaries 与 Completion rule 是不可丢弃保留段。
- 超限时按低到高优先级收缩：global Convention、continuation/role、objective、project Convention、task Convention。
- task/project 规则有最小保留配额；若配置上限连这些规则和安全尾段都无法容纳，编译以 `DomainError` 失败，不派发缺失安全边界的 Context。

### 5. 保持的既有边界

- Host Bridge 仍使用固定 adapter/fixed argv、Bearer token 和项目根 allowlist；没有新增任意 shell/argv 通道。
- Scheduler、探测、恢复、Context 编译和验证仍是 0 Token 控制路径。
- 已有产物路径索引、SHA/大小/修改时间展示、Finder 定位和 Cursor 固定 argv 打开功能被保留。

## 新增或加强的生产路径回归

- Host `token_budget=0` 在 claim/dispatch 前拒绝。
- 两个 Host 任务的 active reservation 阻止全局额度超卖，首个任务结算后释放未使用额度。
- claim 成功但 Host Job 尚未创建时的 stale recovery 释放 reservation。
- Host 外部中断上报用量结算 reservation 并累计到任务。
- 连续两个 Tick 的 Host Job 只能占用一个系统槽；手工 drive 也被事务硬门拒绝。
- Host 项目路径上的 artifact verification 返回 SHA-256、大小、修改时间，文件变化改变 evidence hash。
- 大 global/project/task Convention 下仍保留 task/project 规则、Boundaries 和 Completion rule。
- artifact Finder/Cursor 操作继续验证项目根并使用固定 argv、无 shell。

## 诚实的未修改项

- 没有为真实 CLI 增加 Provider 侧最大输出 Token 截止；当前只保证派发前预算分配不超卖。
- Convention refinement 仍没有任务预算归属，也未进入统一 `token_usage`/reservation；安全实现需要确定每次精炼的预算来源和上限。
- 文件 evidence 已绑定实际内容与元数据，但默认没有要求文件必须由本次 run 新建或修改。对通用 `file_exists` 改成强制变化会破坏“验收既有文件”的合法语义；需要新增显式 provenance policy 后才能安全启用。
- Permission 仍是决策记录，不是任务/命令/网络/秘密引用的生产执行门。
- strict 仍是同一确定性验证器的重复检查，不是真正独立实现或独立审查。
- FaultPolicy 接线、无 PID `dispatch_outcome_unknown` 人工解决动作、前端 SSE、非 loopback 前端认证及报告中的其他 P2/P3 项未在本次范围内修改。
- 未调用真实 Codex、Cursor 或 DeepSeek 服务；真实 CLI 参数兼容、Token 事件语义和费用上限仍需单独上线验证。

## 实际验证结果

在当前未提交工作树上执行：

```text
.venv/bin/python -m pytest
179 passed in 4.49s

cd web && pnpm run lint
eslint . (passed)

cd web && pnpm run typecheck
tsc -b --pretty false (passed)

cd web && pnpm test
1 test file passed, 5 tests passed

cd web && pnpm run build
Vite production build passed; 4558 modules transformed
index.html 0.75 kB (gzip 0.49 kB)
CSS 16.45 kB (gzip 4.37 kB)
JS 300.97 kB (gzip 88.15 kB)
```

最终 `git diff --check` 结果在完成本文件后再次执行并记录为通过；没有创建提交或推送。

CODEX_OPTIMIZATION_COMPLETE
