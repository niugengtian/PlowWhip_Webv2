# PlowWhip 产品需求与问题台账

此台账记录人的需求和实施中发现的问题。它不是 Task、Monitor 或部署状态真源。

## V1 实施覆盖审计（2026-07-24）

审计口径：

- 逐章对照冻结基线 Revision 5、当前源码和可运行测试。
- `已实现` 只表示该章的强制行为均有当前实现和确定性证据。
- `部分实现` 不计为完成；Task 数、页面存在、容器健康和测试总数都不能替代逐项验收。
- §23、§25、§26 是环境对比或治理要求，不计入功能完成率。

当前结论：**17 个功能章节已实现，6 个部分实现，0 个未实现，3 个为对比/治理章节。V1 功能完成声明已撤回：8750 首次真实 Cursor Task 暴露 Host Bridge 隔离、Session bootstrap、确定拒绝分类、待决定恢复和外部 Git 授权缺口。**

| 基线章节 | 状态 | 当前证据 | 明确缺口 |
|---|---|---|---|
| §1 使命 | 已实现 | 自然语言形成 Goal/Task；简单/中型直达执行，大型任务自动形成最小 Plan；模型任务可验证、修复、递补或一次提问 | — |
| §2 部署边界 | 部分实现 | 单镜像、SQLite 持久卷、受限 Host Bridge 代码和跨平台测试已存在 | 现场 8750 实际连接 2026-07-21 启动的旧 `plow_whip_web.host_bridge:8765`，clean-room Bridge 尚未以独立端口/namespace 接入 |
| §3 最终生命周期 | 已实现 | `Intake → Plan → Execute → Verify → Done/NeedsDecision`；执行、Planner、Checker 都使用可恢复 HostJob；compact 事件与 generation 轮转接入 | — |
| §4 状态模型 | 已实现 | 四个公开状态、正交 phase/fault/outcome、取消归档与 rerun generation | — |
| §5 唯一生命周期所有者 | 已实现 | Goal/Task 以及可见 Project 创建/恢复/绑定/归档和项目设置均先进入 messages；只有 `advance_project` 改变运行态；外部调用不占 SQLite 写事务；Monitor 只读 | — |
| §6 业务层级 | 已实现 | Project/Goal/版本化 Plan/Sprint/Task、串行 DAG、同项目单活动 Task；活动期间新主人消息在当前 Task 后、旧 DAG 队列前形成下一紧急 Task | — |
| §7 指令分类与 Planner | 已实现 | 可解释简单/中型/大型判定；大型任务要求两套完整比较方案；置信度 ≥95% 且无授权风险才自动选择并物化 DAG | — |
| §8 管家与统一窗口 | 已实现 | 全局管家可用 `@project`、唯一项目或跨项目精确搜索路由；搜索只转接不创建 Task；全局文件只保存转接引用；项目管家长期历史和一次一个问题 | — |
| §9 Worker/Session/HostJob | 部分实现 | Task+role 唯一 TaskSession、generation 和 HostJob 所有权及持久对账路径已存在 | Cursor 首次执行没有 external session，Adapter 却要求已有 session；Bridge 明确拒绝后本地 HostJob 仍停在 `dispatching` |
| §10 Provider Pool | 部分实现 | 候选顺序、fallback、handoff、Token 归一化和零 Token 探活已接线 | 真实 Cursor 首任务未能 bootstrap；CLI available 不能证明 execution health |
| §11 验证与自动收敛 | 已实现 | 确定性哈希验证；Planner 可冻结 1–20 个细粒度 acceptance；独立 Checker 严格 JSON、逐项 Evidence、有界修复包；只读分析可凭 Evidence 在零 workspace delta 下完成 | — |
| §12 故障/进展/恢复/超时 | 部分实现 | fault、retry、稳定 job_id、deadline/stop_grace 和不盲重放已存在 | Bridge HTTP 400 的确定拒绝被压成通用 `RuntimeError`，随后把 `job not found` 错判为 `unsafe_unknown` 并打扰主人 |
| §13 Cronner | 已实现 | 应用内唯一循环、项目租约、`next_action_at/kind`、deadline、每项目一步、跨项目有界并行、上下文 checkpoint/轮转；共享 data root 单 Cronner 进程锁 | — |
| §14 Monitor | 已实现 | 只读连接、当前结构化状态、最后 20 行、数据库/Cronner/探针看板；页面显示 TaskSession、HostJob、Artifact 计数 | — |
| §15 三层记忆与 Token | 已实现 | Hot Capsule byte cap、Warm handoff 原子归档、Cold Session manifests 只追加；Codex 原生 compact 策略/事件和非原生 Provider Token 阈值 generation 轮转；Token 归一化看板 | — |
| §16 SQLite 与队列 | 已实现 | WAL、十五类权威记录、messages/tasks 队列、授权/配置复用 messages，无第二队列 | — |
| §17 文件目录 | 已实现 | project/task/role/generation、Cold segment、Artifact/Evidence/handoff/library，以及 global/project conversation 投影均在 data root；SQLite 仍是唯一状态真源 | — |
| §18 角色/规则/模板/脚本 | 已实现 | 文件正文 + SQLite revision/SHA 索引；Global/Project/Role/Task 合并；TaskSession 冻结；首次 Checker PASS 自动沉淀不含 Task ID/Secret 的项目模板，“以后都这样”才升 revision；无真实脚本需求时保持确定性命令 | — |
| §19 配置 | 已实现 | Global/Project/Task+role 合并并冻结值与来源；数值阈值、Provider 顺序和项目规则均先入 messages，由 `advance_project` 应用；compact/轮转/观察/超时均有消费者 | — |
| §20 页面和 API | 部分实现 | 七导航、Task 工作台、messages/actions 两类写路由和本地 Session 分段可查看 | 当前 `NeedsDecision` 问题没有可执行的“确认未接收并安全递补”动作；普通决定又会因伪活动 HostJob 被拒绝 |
| §21 权限与不可逆操作 | 部分实现 | 三档权限、作用域授权快照和不可逆默认拒绝路径已存在 | 明确的 GitHub push 指令被分类为 `authorization_required=false`，同时执行 prompt 又一律禁止 commit/外部发送，授权语义自相矛盾 |
| §22 模块边界 | 已实现 | 十个职责模块存在；API→intake→lifecycle→store 与只读 monitor 边界清晰 | — |
| §23 环境对比 | 对比基线 | 旧仓库保持只读；未整体迁入旧状态机 | 不是功能完成项 |
| §24 迁移与蓝绿门禁 | 已实现 | SQLite Backup API + quick_check；候选 code/data/db/Compose/port/Bridge namespace 隔离；候选 Cronner 关闭；共享 data root 单调度锁；切换门禁固定要求主人授权；回滚门禁验证候选调度锁已释放、无活动租约且数据库完整 | 当前是主人指定的全新 clean-room 项目，因此不迁移旧数据；生产切流/回滚执行仍需单独授权 |
| §25 冻结规则 | 治理已落实 | Revision 5 冻结文件保持唯一基线 | 后续不得再用局部 Task 完成冒充 V1 完成 |
| §26 台账 | 治理已落实 | 本文件记录人的需求、问题、决定和证据 | 必须随每一阶段继续更新 |

## 人的需求

| ID | 日期 | 来源 | 需求 | 状态 | 验证证据 |
|---|---|---|---|---|---|
| H-20260723-01 | 2026-07-23 | 主人 | Token 独立导航：全历史/今日五项 Token、两组比值、趋势、项目占比，以及按项目/Task/model/session 的消费明细；session 显示 ID 和 Worker | 已完成 | `/api/token`；Token UI；`test_provider_facts_and_token_normalization_are_fail_closed` |
| H-20260723-02 | 2026-07-23 | 主人 | Monitor 加入导航和独立只读看板 | 已完成 | `/api/monitor`；Monitor UI；`test_project_archive_preserves_history_and_monitor_is_read_only` |
| H-20260723-03 | 2026-07-23 | 主人 | 保留全局管家和项目管家 | 已完成 | 七入口 UI；`/api/butler`；Web API 测试 |
| H-20260723-04 | 2026-07-23 | 主人 | 项目页支持新增、删除及必要基本操作 | 已被修订 | 见 H-20260723-06 |
| H-20260723-05 | 2026-07-23 | 主人 | 持续同步台账，随时记录发现的问题和人的需求 | 已采纳 | 本文件；基线 Revision 5 §26 |
| H-20260723-06 | 2026-07-23 | 主人 | 项目不叫删除，改为归档；归档后不在页面直观显示 | 已完成 | `projects.archived_at`；归档/恢复 API 与 UI；归档测试 |
| H-20260723-07 | 2026-07-23 | 主人 | 复用旧系统已证明的 Provider 0 Token 探针和极小 Token 探针，并归入 Monitor 模块 | 已完成 | 8750 Task `50a80b36468644cca0da6c6d0911e9e6`：Codex CLI available、`model_invoked=false`、全部 Token 为 0、Evidence SHA `7c16ff4f93616b80c2c34ca85188fb1148010edbbd8ffebcd276538963f5084f`；极小 Token 走先持久化 HostJob、释放 SQLite 后派发/对账的假 Bridge 测试，未产生付费调用 |
| H-20260723-08 | 2026-07-23 | 主人 | Task 页参考原任务页补齐驱动与监视能力，并以真实任务验证能否无人值守完成 | 已完成（功能） | 8750 确定性真实 Task `f31fac80250f43cda94b75eb3f11c2d3` 无干预收敛 Done；通用模型路径以真实 HTTP/进程假 Provider 覆盖持久 HostJob、对账、取消、fallback 和结构化 Checker；付费 Provider 实跑仍须单独授权 |
| H-20260723-09 | 2026-07-23 | 主人 | 明确说明 `NeedsDecision` 在哪里、如何处理 | 已完成 | 项目页提供“处理待决定”入口；Task 页只在 `NeedsDecision` 启用主人决定/计划并显示 Fault、等待原因、示例与“取消 Task”；8750 实测 Task `be3b33ff8e224bd4899792a40edbb971` 输入和提交按钮可用 |
| H-20260723-10 | 2026-07-23 | 主人 | Task 页参考原任务页时必须保留任务泳道 | 已完成 | 基线 Revision 4；8750 实测四条公开状态泳道：待决定项目 `0/0/1/0`、探针项目 `0/0/0/3`；卡片点击与详情联动 |
| H-20260723-11 | 2026-07-23 | 主人 | Task 页必须参考原任务页整体重建，不接受只局部增加泳道的毛坯实现 | 已完成 | 8750：顶部指标、Goal 导航、四态泳道、统一 Goal/Task 详情、驱动/决定与运行证据同屏；`design-qa.md` passed |
| H-20260723-12 | 2026-07-23 | 主人 | 所有页面适当沿用原版本 UI；项目范围选择项目后不得自动跳项目页 | 已完成 | 8750 逐页实测七个入口选择范围后 `beforeView == afterView`；显式“进入项目”按钮；基线 Revision 5 |
| H-20260723-13 | 2026-07-23 | 主人 | 不得把阶段完成或少量 Task Done 冒充 V1 完成；继续自动推进全部冻结基线剩余项 | 重新打开 | 真实 Cursor Task 证明此前“23/0/0”结论不成立；当前为 17 已实现/6 部分实现/0 未实现 |
| H-20260724-14 | 2026-07-24 | 主人 | Cursor CLI 可以使用；说明为什么 Token 消耗为 0 | 已完成 | 8750 最新 Cursor Task `8f28f2e6afc448209eef02bad443b55c` 是 0 Token 版本探针，`model_invoked=false`、ModelCallLedger `calls=0`；真实模型 HostJob 才计 Token |
| H-20260724-15 | 2026-07-24 | 主人现场验证 | 首次真实 Cursor Task 不应把系统可判定的启动拒绝包装成主人业务决定 | 待修复 | Task `6e59d69d4a584bce80d8396de2825da7`；HostJob `f1f3064a-8531-4752-8c63-4a0f4655fd21` |

## 发现的问题

| ID | 日期 | 发现 | 影响 | 决定 | 状态 | 验证证据 |
|---|---|---|---|---|---|---|
| I-20260723-01 | 2026-07-23 | 冻结基线原 §20 只允许四个页面区域，与主人新增四项要求冲突 | 未升级基线就实施会产生架构漂移 | 主人明确批准按四项升级；基线升为 Revision 2 | 已解决 | 基线 Revision 2 顶部决定记录与 §20 |
| I-20260723-02 | 2026-07-23 | “今日比值”原文字重复了“全历史” | 今日口径可能歧义 | 使用 Asia/Shanghai 今日 Input/Output 与 Cached-input/Uncached-input | 已决定 | 基线 Revision 2 §15.3 |
| I-20260723-03 | 2026-07-23 | `cached_input_tokens` 是 Input 子集，不能加入 Total | 看板可能重复计费 | Total 固定为 Input + Output；Uncached = Input - Cached | 已解决 | Token 聚合测试：累计快照 167 Total，无重复相加 |
| I-20260723-04 | 2026-07-23 | “删除项目”会销毁消息、Task、Evidence 归属 | 破坏可审计与恢复边界 | 主人修正为归档：保留全部历史，仅从日常列表隐藏；同 ID 创建可恢复 | 已解决 | 归档测试；容器 `archive-check` 验收 |
| I-20260723-05 | 2026-07-23 | 原 `model_calls` 没有 model 字段 | 无法形成真实的按 model 消费明细 | Schema v2 增加可兼容迁移的 `model` 字段，调用记录时冻结实际 model | 已解决 | `PRAGMA user_version=2`；Token model 分组测试 |
| I-20260723-06 | 2026-07-23 | 8750 对真实产品开发指令只能解析为 `kind=unsupported`，Task 在 intake 立即停止 | 当前纵向闭环只能执行 `write <relative-path>: <content>`，不能调度 Worker/Provider 完成代码任务 | 项目先绑定 Host Bridge 白名单内的绝对工作区；普通开发指令创建独立 Fullstack/Checker TaskSession，经工作区前后快照、ModelCallLedger 和只读 Checker Evidence 收敛 | 已解决 | Schema v3 `projects.host_path`；`test_general_code_task_uses_registered_workspace_and_independent_checker` 以假 Bridge 验证 `intake → execute → verify → Done`、双 Session/HostJob、工作区 delta 和 Checker PASS；16 项回归通过；8750 r6 工作区绑定与 0 Token Task `344874368d6e4499a54a5c652d1f1c3a` 实测通过，未调用付费 Provider |
| I-20260723-07 | 2026-07-23 | 当前 Monitor 仅有只读运行态汇总，没有 Provider 分层健康或 0/极小 Token 探针 | 无法从 Monitor 区分 installed、CLI 探活、Session 可恢复与最近真实执行健康 | 保持 Monitor 查询只读；探针只提交普通 Task，极小 Token 探针必须显式确认且不得用探活冒充真实执行 | 已解决 | `/api/monitor.providers`；8750 显示 `installed true · CLI available · resume unknown · execution unknown` 和最新 0 Token Task |
| I-20260723-08 | 2026-07-23 | `NeedsDecision` 页面原来只给一个通用输入框，没有展示可决定问题，且按钮在非待决定状态仍可见 | 主人无法判断“在哪里决定”以及当前是否真的需要决定 | 项目页把入口命名为“处理待决定”；Task 页显示 `wait_reason`/`fault_code`，只在待决定时启用决定与计划，并明确可收窄目标或取消 | 已解决 | 8750 实测 Task `be3b33ff8e224bd4899792a40edbb971`：`Fault=scope`、决定输入/提交均启用；`I-20260723-06` 仍保留为通用代码执行能力缺口 |
| I-20260723-09 | 2026-07-23 | 首次容器化 0 Token 探针报告 Host Bridge unreachable | 容器没有 `host.docker.internal` 到宿主机的映射，Provider 探针只能得到不可达结果 | 容器启动固定加入 `--add-host host.docker.internal:host-gateway`，保留失败 Task 作为历史，不篡改结果 | 已解决 | 失败 Task `9dd5d4147ecc4a68a9667348c86ce415`；修复后 Task `e168cbf8ab284b25ba621b24bd6c0223` 与最终 Task `50a80b36468644cca0da6c6d0911e9e6` 均 `available=true` |
| I-20260723-10 | 2026-07-23 | 已有数据库中来源为 `v1_default` 的 `provider_order` 不会自动补入新 `provider_probe` 角色 | 新 TaskSession 的有效设置快照缺少探针角色默认顺序 | 初始化时只同步 `source=v1_default` 的默认值，保留主人或项目策略 | 已解决 | 最终 Task 两个 Session 的设置快照都含 `provider_probe=[codex_cli,cursor_cli,deepseek,kimi]`；`test_default_settings_upgrade_without_overwriting_project_policy` |
| I-20260723-11 | 2026-07-23 | Task 页只实现了扁平 Task 列表，遗漏原任务页的泳道 | 主人不能一眼看到项目 Task 的四态分布，且“参考原任务页”验收不完整 | 用四态泳道替换重复列表；phase/outcome 等仅作为卡片字段，点击复用原详情 | 已解决 | 8750 浏览器验收：两项目归类与计数正确、选中态和详情同步、无控制台错误、无内容越界 |
| I-20260723-12 | 2026-07-23 | 任务页把泳道放在旧详情面板上方，只修了局部结构 | 页面缺少原版的 Goal 上下文、核心指标和同屏详情，信息密度低且操作路径割裂 | 整页替换为指标 + Goal 导航 + 四泳道 + 右侧详情的单一工作台；保留 V1 四态和现有写入口 | 已解决 | 1280×720 同尺寸比较、Goal/Task 联动和四泳道无溢出通过；`design-qa.md` |
| I-20260723-13 | 2026-07-23 | 项目范围选择器直接调用 `openProject` | 用户在 Task、Token、Monitor 等页选择范围时被强制切走，筛选与导航语义混淆 | 选择器只更新 `currentProject` 并刷新当前页；另设显式“进入项目”按钮 | 已解决 | 8750 逐页实测全局管家、项目管家、项目、Task、Token、Monitor、设置页均原位刷新 |
| I-20260723-14 | 2026-07-23 | Monitor 枚举 Session 文件后再 `stat` 时，执行器可能已原子替换并移除临时文件 | 只读 Task 快照偶发 `FileNotFoundError`，HTTP 请求被断开 | 有界枚举时跳过已经消失的临时文件；不重试、不写状态、不扩大日志读取 | 已解决 | 并发 Web 回归复现后修复；15 项回归连续通过 |
| I-20260723-15 | 2026-07-23 | CSS 只声明了 `section[hidden]`，Goal 历史和详情切换使用的普通 `div[hidden]` 仍被组件 `display` 覆盖 | 右侧 Task 详情与空状态同时显示，已完成 Goal 区也会泄漏隐藏内容 | 全局统一 `[hidden]{display:none!important}`，保持原生 hidden 语义 | 已解决 | 1280×720 实机截图发现并修复；Web UI 回归固定 hidden 规则 |
| I-20260723-16 | 2026-07-23 | Provider 执行和 Checker 原来在 `advance_project` 的 SQLite 写事务与 30 秒项目租约内同步运行 | 长任务会阻塞 Cronner/API 写入、租约过期；无法可靠取消、重启恢复或让不同项目并行 | 执行、Planner、Checker 全部改为持久 HostJob start/status/output/cancel；外部调用移出事务；稳定 job_id 可跨 Store/进程重启继续轮询 | 已解决 | `test_running_host_job_releases_sqlite_and_can_reconcile_or_cancel` 同时验证 Executor/Checker 释放 SQLite 与 Checker 重启恢复；`test_running_planner_host_job_reconciles_after_store_restart` |
| I-20260723-17 | 2026-07-23 | Provider 候选顺序原来只被保存，实际通用代码任务默认并固定 `codex_cli` | 普通 Provider 故障直接 NeedsDecision，违反自动递补与 generation 连续性 | Executor、Planner、Checker 都按冻结候选顺序运行；明确终态失败归档 generation、保留 Task/TaskSession/预算/handoff 并自动递补；Project Provider 顺序可入队配置 | 已解决 | Executor、Planner、Checker 三条 fallback 回归；`test_project_provider_order_and_rules_freeze_into_task_session` |
| I-20260723-18 | 2026-07-23 | 通用代码任务原来只生成 `independent_checker_pass` 一个 acceptance，并以字符串 marker 判 PASS | 无法证明主人的每项验收要求；失败不能形成可执行的最小修复包 | 通用合同冻结主人指令/相关检查；Planner 可为每个 Task 给出 1–20 项稳定 acceptance；Checker 严格 JSON、逐项 Evidence、CHANGES_REQUIRED 有界修复包 | 已解决 | `test_checker_changes_required_is_a_bounded_repair_package`；Planner 细粒度 acceptance 测试；代码任务集成测试 |
| I-20260723-19 | 2026-07-23 | 任意非写入/探针指令直接视为中型代码任务；大型 Plan 只能由主人手工提交 JSON | Butler/Planner 无法按真实复杂度自动形成最小方案和 DAG | 已实现可解释分类；大型任务由只读 Planner 生成至少两套方案，≥95% 且无授权风险自动选中，否则项目管家一次只问一个问题 | 已解决 | `test_large_instruction_uses_planner_and_auto_selects_confident_plan`、`test_high_risk_plan_asks_exactly_one_project_butler_question`；25 项回归通过 |
| I-20260723-20 | 2026-07-23 | 原 retry 直接重跑，没有先对账 HostJob/Session/workspace/Evidence/handoff；超时只传给同步 Bridge | 中断或返回不明确时可能重复执行，尤其不能保护不可逆动作 | 稳定 job_id 有界对账；模糊派发耗尽后保持未知 HostJob 并停到 unsafe_unknown；Planner/Checker 同样恢复；deadline 先 reconcile、按 stop_grace 取消，Tick 后保存 handoff | 已解决 | 模糊派发、跨重启 Planner/Checker、deadline graceful stop 三组回归 |
| I-20260723-21 | 2026-07-23 | 只有 Warm `current.json` handoff 与归档 revision | 长期任务缺少 Hot Capsule、Cold 分段、轮转和 Provider compact 协调 | Hot 临时 Capsule、Warm 原子归档、Cold append-only Session manifest；Codex 原生 compact 阈值下传并记录事件；非原生 Provider 超阈值在安全边界生成新 generation | 已解决 | `test_hot_warm_cold_continuity_is_bounded_and_append_only`；`test_context_policy_compaction_event_and_non_native_rotation` |
| I-20260723-22 | 2026-07-23 | 不可逆权限仅靠固定 prompt 和少量确认，未保存绑定范围的授权事实 | Session 替换后不能安全继承授权，也不能证明授权已在终态失效 | 三档权限进入角色快照；Planner 方案授权复用 messages 并绑定 project/task/revision/action/scope/expiry；普通决定不能绕过；未实现的外部影响动作继续硬拒绝 | 已解决 | `test_high_risk_plan_asks_exactly_one_project_butler_question`；read-only 分析 HostJob 验证 `access=read` |
| I-20260723-23 | 2026-07-23 | 设置与资源库只有只读页；Project 规则、模板/脚本晋升和多项阈值没有执行路径 | TaskSession 快照看似完整但不能形成可维护的业务配置闭环 | 数值阈值、Provider 顺序、Project 规则均走 messages/advance；新 Session 冻结来源；首次 PASS 自动沉淀项目模板，“以后都这样”才升 revision | 已解决 | `test_project_provider_order_and_rules_freeze_into_task_session`；项目模板回归；设置 UI |
| I-20260723-24 | 2026-07-23 | 没有一致性备份、候选隔离、生产调度租约切换和回滚工具 | 无法满足 §24 蓝绿切换门禁；当前 8750 只能算本地候选演示 | 实现 SQLite Backup API、候选六项隔离清单、候选 Cronner 默认无权、共享 data root 调度锁；门禁不授予切流，生产动作仍需主人单独批准 | 部分解决 | `test_backup_candidate_isolation_and_single_scheduler_gate`；本阶段未触碰旧数据或生产 |
| I-20260723-25 | 2026-07-23 | 曾用“台账显式待处理为 0”和“7/7 Task Done”宣称 V1 完成 | 混淆局部运行结果与冻结基线覆盖，导致遗漏未被记录 | 永久使用本章逐项覆盖审计；部分实现不得计完成；每次报告同时列出未做项 | 已解决 | 本次 26 章覆盖矩阵和 I-20260723-16 至 24 |
| I-20260723-26 | 2026-07-23 | Project 创建/绑定/归档和设置原由 API 直接写最终状态 | 绕过唯一生命周期所有者，重启时也无法从消息队列重放未处理意图 | 新 Project 先保存为隐藏队列宿主；创建/恢复/绑定/归档/设置全部写 messages action，只由 `advance_project` 应用可见状态 | 已解决 | Project 归档、Web API、设置与 Planner 项目测试；25 项回归通过 |
| I-20260723-27 | 2026-07-23 | 通用 Checker 把“没有 workspace delta”一律判失败 | 只读查询、审查和分析即使有独立 Evidence 也无法 Done，违反真实进展规则 | Intake 冻结 `workspace_change_required`；只读分析的 Executor HostJob 使用 `access=read`，Checker 合同和 Evidence 可在零 delta 下通过 | 已解决 | `test_read_only_analysis_can_finish_from_evidence_without_workspace_delta` |
| I-20260723-28 | 2026-07-23 | 全局窗口要求手填项目且全局文件复制完整消息正文 | 不能按搜索结果路由；全局历史和项目历史重复内容 | 支持 `@project`、唯一项目和唯一搜索命中路由；搜索只写 `global_route` 引用不创建 Task；全局文件只含 message_id/project/time | 已解决 | `test_global_butler_routes_search_without_creating_a_task` |
| I-20260723-29 | 2026-07-23 | 初版自动沉淀模板正文含 Task ID/Evidence 路径 | 临时 Task 身份会污染长期 Worker 模板 | 模板正文只保留稳定角色约束，revision 文件不可变；Task/Evidence 只留在审计事件，不进入模板 | 已解决 | `test_message_to_verified_done`；模板文件正文检查 |
| I-20260723-30 | 2026-07-23 | 多镜像共享数据库时只有 Project 租约，没有实例级调度所有权 | 两个镜像可分别领取不同 Project，不满足任一时刻单一生产 Cronner | 同一 data root 使用标准库 `flock` 获得实例级调度锁；候选服务可显式 `--cronner-disabled` | 已解决 | `test_backup_candidate_isolation_and_single_scheduler_gate` |
| I-20260723-31 | 2026-07-23 | 当前仓库原来只有 Host Bridge 客户端合同，没有可运行的宿主服务 | V1 不能独立创建、恢复、观察或取消真实 HostJob，只能依赖旧环境偶然存在的服务 | 只参考旧实现的最小已证明约束，使用标准库重写受限 Host Bridge；不复制旧服务、状态机或 UI | 已解决 | `test_restricted_durable_job_and_restart_recovery`、`test_scope_executable_loopback_and_cancel_guards` 运行真实 HTTP 和本地假进程，验证固定回环/Token/root/Adapter、先落盘、幂等、退出码、有界输出、取消和重启恢复 |
| I-20260723-32 | 2026-07-23 | 极小 Token 探针原来在 `advance_project` 的 SQLite 写事务里同步调用一次性 `/v1/execute` | 探针期间阻塞所有写入，且进程结果没有持久 HostJob 所有权，重启可能重复付费调用 | 0 Token 和极小 Token 都先写 HostJob；外部调用在事务外执行；极小探针复用 jobs start/status/output/cancel 对账，只有明确终态才记录结果 | 已解决 | `test_provider_probe_tasks_record_zero_and_minimal_token_evidence` 在每次 Bridge 请求内另开 SQLite 写事务并成功；失去退出码按失败处理 |
| I-20260723-33 | 2026-07-23 | Monitor API 已有 TaskSession/HostJob/Artifact，页面没有显示；Task API 已有本地 Session 文件，详情没有入口 | 主人只能看到汇总或数据库事实，不能在对应看板观察这些闭环证据 | 直接复用现有只读返回值增加四个小视图，无新 API、状态或服务 | 已解决 | Web 回归断言 Monitor 三项计数和 Task `task-session-files`；35 项回归通过 |
| I-20260724-34 | 2026-07-24 | Linux slim 没有 `ps`，Host Bridge 无法证明重启后 PID 是否仍是原进程 | 仍存活的 HostJob 可能被误判为 `interrupted`，破坏跨平台恢复 | Linux 优先读取 `/proc/<pid>/stat` 的进程启动身份，macOS 保留 `ps lstart`；身份不可证明时继续安全失败 | 已解决 | 同一活动假进程在 Linux slim 和 macOS 宿主均通过 Bridge 重启、`orphan_running` 对账与取消测试 |
| I-20260724-35 | 2026-07-24 | Cursor CLI 虽可探活，但 Host Bridge 拒绝其只读 Planner/Checker；Provider 又写死 macOS App 路径；页面的 0 Token 容易被误解为漏计 | Cursor fallback 和 Linux Host Bridge 不可用，且 CLI availability 与模型用量语义混淆 | Cursor 只读任务使用 `--mode plan` 且不带 `--force`，写任务才启用；Adapter 从 PATH 解析并兼容 `cursor-agent`；Cursor 可提交需再次确认的极小 Token 探针；补累计 Token/缓存子集归一化测试，Monitor 明示零探针未调用模型 | 已解决 | 本机 `cursor agent --help/create-chat --help` 与 Codex/Cursor 0 Token 探活；`test_cursor_read_mode_and_cumulative_token_normalization`；Cursor minimal intake 合同 |
| I-20260724-36 | 2026-07-24 | 候选隔离门禁没有实现基线要求的回滚前调度锁和租约释放确认 | 即使候选 Cronner 仍在运行，也可能错误开始回滚 | 新增只读 `rollback-preflight`：要求 manifest 声明 Cronner disabled、同一 `.cronner.lock` 可取得、SQLite quick_check 通过且活动租约为 0；不执行切流 | 已解决 | `test_backup_candidate_isolation_and_single_scheduler_gate` 覆盖 ready、锁仍占用和活动租约三种事实 |
| I-20260724-37 | 2026-07-24 | 首次真实 Cursor Task 的 Bridge start 未被接收，Bridge 后续明确返回 `host job not found`；客户端丢失 HTTP 400 正文并按不明结果重试两次 | 本可自动判定“未开始”的失败被错误升级为 `unsafe_unknown / NeedsDecision`；取消和普通决定都无法安全解除伪活动 HostJob | 隔离接入 clean-room Bridge；保留 HTTP 拒绝分类；实现 Cursor 新 Session bootstrap；为“明确未接收”增加自动失败与新 generation 递补；Git 远端写入按目标/分支冻结授权 | 待修复 | 8750 SQLite HostJob 仍为 `dispatching`；Bridge `/v1/jobs/status` 返回 HTTP 400 `host job not found`；监听 8765 的 PID 90968 是旧 `plow_whip_web.host_bridge` |
