# PlowWhip 产品需求与问题台账

此台账记录人的需求和实施中发现的问题。它不是 Task、Monitor 或部署状态真源。

## 人的需求

| ID | 日期 | 来源 | 需求 | 状态 | 验证证据 |
|---|---|---|---|---|---|
| H-20260723-01 | 2026-07-23 | 主人 | Token 独立导航：全历史/今日五项 Token、两组比值、趋势、项目占比，以及按项目/Task/model/session 的消费明细；session 显示 ID 和 Worker | 已完成 | `/api/token`；Token UI；`test_provider_facts_and_token_normalization_are_fail_closed` |
| H-20260723-02 | 2026-07-23 | 主人 | Monitor 加入导航和独立只读看板 | 已完成 | `/api/monitor`；Monitor UI；`test_project_archive_preserves_history_and_monitor_is_read_only` |
| H-20260723-03 | 2026-07-23 | 主人 | 保留全局管家和项目管家 | 已完成 | 七入口 UI；`/api/butler`；Web API 测试 |
| H-20260723-04 | 2026-07-23 | 主人 | 项目页支持新增、删除及必要基本操作 | 已被修订 | 见 H-20260723-06 |
| H-20260723-05 | 2026-07-23 | 主人 | 持续同步台账，随时记录发现的问题和人的需求 | 已采纳 | 本文件；基线 Revision 4 §26 |
| H-20260723-06 | 2026-07-23 | 主人 | 项目不叫删除，改为归档；归档后不在页面直观显示 | 已完成 | `projects.archived_at`；归档/恢复 API 与 UI；归档测试 |
| H-20260723-07 | 2026-07-23 | 主人 | 复用旧系统已证明的 Provider 0 Token 探针和极小 Token 探针，并归入 Monitor 模块 | 已完成 | 8750 Task `50a80b36468644cca0da6c6d0911e9e6`：Codex CLI available、`model_invoked=false`、全部 Token 为 0、Evidence SHA `7c16ff4f93616b80c2c34ca85188fb1148010edbbd8ffebcd276538963f5084f`；极小 Token 路径仅用假 Bridge 自动测试，未产生付费调用 |
| H-20260723-08 | 2026-07-23 | 主人 | Task 页参考原任务页补齐驱动与监视能力，并以真实任务验证能否无人值守完成 | 已完成 | 8750 显示四态泳道、两个成功 HostJob、TaskSession/Worker、Artifact/Evidence、等待原因与操作；Task `50a80b36468644cca0da6c6d0911e9e6` 自动收敛 Done；15 项回归通过 |
| H-20260723-09 | 2026-07-23 | 主人 | 明确说明 `NeedsDecision` 在哪里、如何处理 | 已完成 | 项目页提供“处理待决定”入口；Task 页只在 `NeedsDecision` 启用主人决定/计划并显示 Fault、等待原因、示例与“取消 Task”；8750 实测 Task `be3b33ff8e224bd4899792a40edbb971` 输入和提交按钮可用 |
| H-20260723-10 | 2026-07-23 | 主人 | Task 页参考原任务页时必须保留任务泳道 | 已完成 | 基线 Revision 4；8750 实测四条公开状态泳道：待决定项目 `0/0/1/0`、探针项目 `0/0/0/3`；卡片点击与详情联动 |

## 发现的问题

| ID | 日期 | 发现 | 影响 | 决定 | 状态 | 验证证据 |
|---|---|---|---|---|---|---|
| I-20260723-01 | 2026-07-23 | 冻结基线原 §20 只允许四个页面区域，与主人新增四项要求冲突 | 未升级基线就实施会产生架构漂移 | 主人明确批准按四项升级；基线升为 Revision 2 | 已解决 | 基线 Revision 2 顶部决定记录与 §20 |
| I-20260723-02 | 2026-07-23 | “今日比值”原文字重复了“全历史” | 今日口径可能歧义 | 使用 Asia/Shanghai 今日 Input/Output 与 Cached-input/Uncached-input | 已决定 | 基线 Revision 2 §15.3 |
| I-20260723-03 | 2026-07-23 | `cached_input_tokens` 是 Input 子集，不能加入 Total | 看板可能重复计费 | Total 固定为 Input + Output；Uncached = Input - Cached | 已解决 | Token 聚合测试：累计快照 167 Total，无重复相加 |
| I-20260723-04 | 2026-07-23 | “删除项目”会销毁消息、Task、Evidence 归属 | 破坏可审计与恢复边界 | 主人修正为归档：保留全部历史，仅从日常列表隐藏；同 ID 创建可恢复 | 已解决 | 归档测试；容器 `archive-check` 验收 |
| I-20260723-05 | 2026-07-23 | 原 `model_calls` 没有 model 字段 | 无法形成真实的按 model 消费明细 | Schema v2 增加可兼容迁移的 `model` 字段，调用记录时冻结实际 model | 已解决 | `PRAGMA user_version=2`；Token model 分组测试 |
| I-20260723-06 | 2026-07-23 | 8750 对真实产品开发指令只能解析为 `kind=unsupported`，Task 在 intake 立即停止 | 当前纵向闭环只能执行 `write <relative-path>: <content>`，不能调度 Worker/Provider 完成代码任务 | 保留真实 `NeedsDecision`，不得改写成占位文件成功；后续需新增最小的受限代码执行路径 | 待处理 | Task `be3b33ff8e224bd4899792a40edbb971`：`fault_code=scope`；TaskSession/HostJob/Artifact/ModelCall 均为 0 |
| I-20260723-07 | 2026-07-23 | 当前 Monitor 仅有只读运行态汇总，没有 Provider 分层健康或 0/极小 Token 探针 | 无法从 Monitor 区分 installed、CLI 探活、Session 可恢复与最近真实执行健康 | 保持 Monitor 查询只读；探针只提交普通 Task，极小 Token 探针必须显式确认且不得用探活冒充真实执行 | 已解决 | `/api/monitor.providers`；8750 显示 `installed true · CLI available · resume unknown · execution unknown` 和最新 0 Token Task |
| I-20260723-08 | 2026-07-23 | `NeedsDecision` 页面原来只给一个通用输入框，没有展示可决定问题，且按钮在非待决定状态仍可见 | 主人无法判断“在哪里决定”以及当前是否真的需要决定 | 项目页把入口命名为“处理待决定”；Task 页显示 `wait_reason`/`fault_code`，只在待决定时启用决定与计划，并明确可收窄目标或取消 | 已解决 | 8750 实测 Task `be3b33ff8e224bd4899792a40edbb971`：`Fault=scope`、决定输入/提交均启用；`I-20260723-06` 仍保留为通用代码执行能力缺口 |
| I-20260723-09 | 2026-07-23 | 首次容器化 0 Token 探针报告 Host Bridge unreachable | 容器没有 `host.docker.internal` 到宿主机的映射，Provider 探针只能得到不可达结果 | 容器启动固定加入 `--add-host host.docker.internal:host-gateway`，保留失败 Task 作为历史，不篡改结果 | 已解决 | 失败 Task `9dd5d4147ecc4a68a9667348c86ce415`；修复后 Task `e168cbf8ab284b25ba621b24bd6c0223` 与最终 Task `50a80b36468644cca0da6c6d0911e9e6` 均 `available=true` |
| I-20260723-10 | 2026-07-23 | 已有数据库中来源为 `v1_default` 的 `provider_order` 不会自动补入新 `provider_probe` 角色 | 新 TaskSession 的有效设置快照缺少探针角色默认顺序 | 初始化时只同步 `source=v1_default` 的默认值，保留主人或项目策略 | 已解决 | 最终 Task 两个 Session 的设置快照都含 `provider_probe=[codex_cli,cursor_cli,deepseek,kimi]`；`test_default_settings_upgrade_without_overwriting_project_policy` |
| I-20260723-11 | 2026-07-23 | Task 页只实现了扁平 Task 列表，遗漏原任务页的泳道 | 主人不能一眼看到项目 Task 的四态分布，且“参考原任务页”验收不完整 | 用四态泳道替换重复列表；phase/outcome 等仅作为卡片字段，点击复用原详情 | 已解决 | 8750 浏览器验收：两项目归类与计数正确、选中态和详情同步、无控制台错误、无内容越界 |
