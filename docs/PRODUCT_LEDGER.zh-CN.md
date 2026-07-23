# PlowWhip 产品需求与问题台账

此台账记录人的需求和实施中发现的问题。它不是 Task、Monitor 或部署状态真源。

## 人的需求

| ID | 日期 | 来源 | 需求 | 状态 | 验证证据 |
|---|---|---|---|---|---|
| H-20260723-01 | 2026-07-23 | 主人 | Token 独立导航：全历史/今日五项 Token、两组比值、趋势、项目占比，以及按项目/Task/model/session 的消费明细；session 显示 ID 和 Worker | 已完成 | `/api/token`；Token UI；`test_provider_facts_and_token_normalization_are_fail_closed` |
| H-20260723-02 | 2026-07-23 | 主人 | Monitor 加入导航和独立只读看板 | 已完成 | `/api/monitor`；Monitor UI；`test_project_archive_preserves_history_and_monitor_is_read_only` |
| H-20260723-03 | 2026-07-23 | 主人 | 保留全局管家和项目管家 | 已完成 | 七入口 UI；`/api/butler`；Web API 测试 |
| H-20260723-04 | 2026-07-23 | 主人 | 项目页支持新增、删除及必要基本操作 | 已被修订 | 见 H-20260723-06 |
| H-20260723-05 | 2026-07-23 | 主人 | 持续同步台账，随时记录发现的问题和人的需求 | 已采纳 | 本文件；基线 Revision 2 §26 |
| H-20260723-06 | 2026-07-23 | 主人 | 项目不叫删除，改为归档；归档后不在页面直观显示 | 已完成 | `projects.archived_at`；归档/恢复 API 与 UI；归档测试 |
| H-20260723-07 | 2026-07-23 | 主人 | 复用旧系统已证明的 Provider 0 Token 探针和极小 Token 探针，并归入 Monitor 模块 | 待实现 | 8750 项目 `plowwhip-unattended-probe`；Task `be3b33ff8e224bd4899792a40edbb971` |
| H-20260723-08 | 2026-07-23 | 主人 | Task 页参考原任务页补齐驱动与监视能力，并以真实任务验证能否无人值守完成 | 待实现 | 8750 项目 `plowwhip-unattended-probe`；本次无人值守实测 |

## 发现的问题

| ID | 日期 | 发现 | 影响 | 决定 | 状态 | 验证证据 |
|---|---|---|---|---|---|---|
| I-20260723-01 | 2026-07-23 | 冻结基线原 §20 只允许四个页面区域，与主人新增四项要求冲突 | 未升级基线就实施会产生架构漂移 | 主人明确批准按四项升级；基线升为 Revision 2 | 已解决 | 基线 Revision 2 顶部决定记录与 §20 |
| I-20260723-02 | 2026-07-23 | “今日比值”原文字重复了“全历史” | 今日口径可能歧义 | 使用 Asia/Shanghai 今日 Input/Output 与 Cached-input/Uncached-input | 已决定 | 基线 Revision 2 §15.3 |
| I-20260723-03 | 2026-07-23 | `cached_input_tokens` 是 Input 子集，不能加入 Total | 看板可能重复计费 | Total 固定为 Input + Output；Uncached = Input - Cached | 已解决 | Token 聚合测试：累计快照 167 Total，无重复相加 |
| I-20260723-04 | 2026-07-23 | “删除项目”会销毁消息、Task、Evidence 归属 | 破坏可审计与恢复边界 | 主人修正为归档：保留全部历史，仅从日常列表隐藏；同 ID 创建可恢复 | 已解决 | 归档测试；容器 `archive-check` 验收 |
| I-20260723-05 | 2026-07-23 | 原 `model_calls` 没有 model 字段 | 无法形成真实的按 model 消费明细 | Schema v2 增加可兼容迁移的 `model` 字段，调用记录时冻结实际 model | 已解决 | `PRAGMA user_version=2`；Token model 分组测试 |
| I-20260723-06 | 2026-07-23 | 8750 对真实产品开发指令只能解析为 `kind=unsupported`，Task 在 intake 立即停止 | 当前纵向闭环只能执行 `write <relative-path>: <content>`，不能调度 Worker/Provider 完成代码任务 | 保留真实 `NeedsDecision`，不得改写成占位文件成功；后续需新增最小的受限代码执行路径 | 待处理 | Task `be3b33ff8e224bd4899792a40edbb971`：`fault_code=scope`；TaskSession/HostJob/Artifact/ModelCall 均为 0 |
| I-20260723-07 | 2026-07-23 | 当前 Monitor 仅有只读运行态汇总，没有 Provider 分层健康或 0/极小 Token 探针 | 无法从 Monitor 区分 installed、CLI 探活、Session 可恢复与最近真实执行健康 | 保持 Monitor 只读；探针结果作为事实输入展示，极小 Token 探针必须显式触发且不得用探活冒充真实执行 | 待处理 | `/api/monitor` 返回 `read_only=true`，本次任务无 Provider/Token 记录 |
