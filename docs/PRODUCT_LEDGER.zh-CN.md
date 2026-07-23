# PlowWhip 产品需求与问题台账

此台账记录人的需求和实施中发现的问题。它不是 Task、Monitor 或部署状态真源。

## V1 实施覆盖审计（2026-07-23）

审计口径：

- 逐章对照冻结基线 Revision 5、当前源码和可运行测试。
- `已实现` 只表示该章的强制行为均有当前实现和确定性证据。
- `部分实现` 不计为完成；Task 数、页面存在、容器健康和测试总数都不能替代逐项验收。
- §23、§25、§26 是环境对比或治理要求，不计入功能完成率。

当前结论：**4 个功能章节已实现，17 个部分实现，2 个未实现，3 个为对比/治理章节。V1 未完成。**

| 基线章节 | 状态 | 当前证据 | 明确缺口 |
|---|---|---|---|
| §1 使命 | 部分实现 | 自然语言可形成 Task；确定性任务与假 Bridge 代码任务可收敛 | 项目管家澄清、自动最小 Plan、完整自动修复收敛未实现 |
| §2 部署边界 | 部分实现 | 单镜像内含 Web/API/UI/Cronner/Monitor；SQLite 持久卷；Host Bridge HTTP 适配 | 没有生产调度租约门禁、Host Bridge 常驻安装与跨平台验收 |
| §3 最终生命周期 | 部分实现 | `Intake → Execute → Verify → Done/NeedsDecision` 可运行 | 自动 Planner、Provider fallback、compact、完整恢复链未实现 |
| §4 状态模型 | 已实现 | 四个公开状态、正交 phase/fault/outcome、取消归档与 rerun generation | — |
| §5 唯一生命周期所有者 | 部分实现 | Goal/Task 迁移经 `advance_project`；外部 Provider/Checker 调用不占 SQLite 写事务；Monitor 只读 | Project 创建/归档直接写入而非只提交意图 |
| §6 业务层级 | 部分实现 | Project/Goal/Plan/Task、Sprint 分组、DAG、同项目单活动 Task | 没有自动生成/选择 Plan；active Goal 排队和插队规则缺少完整产品行为 |
| §7 指令分类与 Planner | 部分实现 | 确定性写入与通用代码任务可区分；手工大型 Plan 有校验 | 没有自动简单/中型/大型判定、两套方案生成和 95% 自动选择 |
| §8 管家与统一窗口 | 部分实现 | 全局搜索、项目历史和两个入口已存在 | 没有原窗口全局代理转接、一次一个澄清问题和语义归纳 |
| §9 Worker/Session/HostJob | 部分实现 | 所有权表与 Task+role 唯一约束；HostJob 可持久表示 dispatching/running/cancelling/终态；执行器可对账和停止 | 旧 Bridge 的只读 Checker 仍是同步调用；中断后只会安全停下，不能恢复原 Checker 进程 |
| §10 Provider Pool | 部分实现 | TaskSession 冻结有序候选；Fullstack 从 Cursor 起步，明确终态失败后归档 generation 并递补 Codex；Task/预算不重置 | Checker 仅 Codex 支持只读；无完整可用性选择、compact/resume 和中断 generation 恢复 |
| §11 验证与自动收敛 | 部分实现 | 确定性哈希验证；独立 Checker 使用严格 JSON 合同；两个冻结 acceptance 分别落 Evidence；CHANGES_REQUIRED 生成有界修复包 | Planner 尚不能把主人自然语言拆成全部细粒度 acceptance；NEEDS_DECISION 语义仍较基础 |
| §12 故障/进展/恢复/超时 | 部分实现 | fault 字段、有限 retry、工作区 delta/Evidence；执行 HostJob 按稳定 job_id 对账、取消、终态 Provider 递补并保留有界输出 | Checker 进程恢复、deadline/stop_grace 后完整 handoff 和不可逆结果保护仍不完整 |
| §13 Cronner | 部分实现 | 应用内唯一循环、租约、到期动作、每次每项目一个推进动作；不同项目使用标准库有界线程并行 | 缺少完整声明式观察/会话阈值执行；长 Checker 仍依赖延长项目租约 |
| §14 Monitor | 已实现 | 只读连接、当前结构化状态、最后 20 行、数据库/Cronner/探针看板 | — |
| §15 三层记忆与 Token | 部分实现 | 有界 Warm handoff、archive revision、Session 文件和 Token 归一化看板 | 无 Hot Context Capsule、Cold 分段轮转、原生 compact 协调和上下文阈值 |
| §16 SQLite 与队列 | 已实现 | WAL、十五类权威记录、messages/tasks 队列、无第二队列 | — |
| §17 文件目录 | 部分实现 | project/task/role/generation、Artifact/Evidence/handoff 和 library 均在 data root | global/project conversation 文件与完整 Cold session 分段/轮转未实现 |
| §18 角色/规则/模板/脚本 | 部分实现 | 默认文件库、SHA/revision 索引、TaskSession 快照 | Project 规则合并、成功后模板/脚本沉淀与跨项目确认未实现 |
| §19 配置 | 部分实现 | Global/Project/Task+role 合并并冻结来源；主要上限有校验 | 无配置写入口；Provider/compact/轮转等多项设置未被运行时执行 |
| §20 页面和 API | 部分实现 | 七导航、完整 Task 工作台、messages/actions 两类写路由 | Project action 仍直接改变项目；本地完整会话分段尚无真实数据 |
| §21 权限与不可逆操作 | 未实现 | 只有固定安全 prompt、路径约束和归档确认 | 无绑定 project/task/revision/action/scope/expiry 的临时授权事实和失效机制 |
| §22 模块边界 | 已实现 | 十个职责模块存在；API→intake→lifecycle→store 与只读 monitor 边界清晰 | — |
| §23 环境对比 | 对比基线 | 旧仓库保持只读；未整体迁入旧状态机 | 不是功能完成项 |
| §24 迁移与蓝绿门禁 | 未实现 | 当前仅保留本地 Docker 回滚容器 | 一致性备份、候选隔离、调度租约切换、回滚和旧数据 reconcile 均未实现 |
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
| H-20260723-07 | 2026-07-23 | 主人 | 复用旧系统已证明的 Provider 0 Token 探针和极小 Token 探针，并归入 Monitor 模块 | 已完成 | 8750 Task `50a80b36468644cca0da6c6d0911e9e6`：Codex CLI available、`model_invoked=false`、全部 Token 为 0、Evidence SHA `7c16ff4f93616b80c2c34ca85188fb1148010edbbd8ffebcd276538963f5084f`；极小 Token 路径仅用假 Bridge 自动测试，未产生付费调用 |
| H-20260723-08 | 2026-07-23 | 主人 | Task 页参考原任务页补齐驱动与监视能力，并以真实任务验证能否无人值守完成 | 部分完成 | UI 与 0 Token Probe Task 已真实自动收敛；通用代码路径已用假 Bridge 覆盖持久 HostJob、对账、取消、fallback 和结构化 Checker，但尚未运行真实付费 Provider 任务 |
| H-20260723-09 | 2026-07-23 | 主人 | 明确说明 `NeedsDecision` 在哪里、如何处理 | 已完成 | 项目页提供“处理待决定”入口；Task 页只在 `NeedsDecision` 启用主人决定/计划并显示 Fault、等待原因、示例与“取消 Task”；8750 实测 Task `be3b33ff8e224bd4899792a40edbb971` 输入和提交按钮可用 |
| H-20260723-10 | 2026-07-23 | 主人 | Task 页参考原任务页时必须保留任务泳道 | 已完成 | 基线 Revision 4；8750 实测四条公开状态泳道：待决定项目 `0/0/1/0`、探针项目 `0/0/0/3`；卡片点击与详情联动 |
| H-20260723-11 | 2026-07-23 | 主人 | Task 页必须参考原任务页整体重建，不接受只局部增加泳道的毛坯实现 | 已完成 | 8750：顶部指标、Goal 导航、四态泳道、统一 Goal/Task 详情、驱动/决定与运行证据同屏；`design-qa.md` passed |
| H-20260723-12 | 2026-07-23 | 主人 | 所有页面适当沿用原版本 UI；项目范围选择项目后不得自动跳项目页 | 已完成 | 8750 逐页实测七个入口选择范围后 `beforeView == afterView`；显式“进入项目”按钮；基线 Revision 5 |

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
| I-20260723-16 | 2026-07-23 | Provider 执行和 Checker 原来在 `advance_project` 的 SQLite 写事务与 30 秒项目租约内同步运行 | 长任务会阻塞 Cronner/API 写入、租约过期；无法可靠取消、重启恢复或让不同项目并行 | 执行器改为持久 HostJob snapshot/dispatch/poll/cancel；外部调用全部移出 SQLite 写事务；Cronner 跨项目并行。旧 Bridge 不支持只读异步 Checker，因此 Checker 中断先 fail-closed，待 Bridge 最小契约补齐后再恢复 | 部分解决 | Schema v4；`test_running_host_job_releases_sqlite_and_can_reconcile_or_cancel`；`test_schema_v4_preserves_terminal_jobs_and_accepts_running_jobs`；18 项回归通过 |
| I-20260723-17 | 2026-07-23 | Provider 候选顺序原来只被保存，实际通用代码任务默认并固定 `codex_cli` | 普通 Provider 故障直接 NeedsDecision，违反自动递补与 generation 连续性 | 已按冻结 TaskSession 设置选择 Fullstack 候选；明确终态故障安全归档旧 generation 并递补下一 Provider。Checker 非 Codex 只读适配与 compact/resume 尚待补齐 | 部分解决 | `test_terminal_provider_failure_falls_back_with_new_generation`：Cursor generation 1 失败后 Codex generation 2 完成；19 项回归通过 |
| I-20260723-18 | 2026-07-23 | 通用代码任务原来只生成 `independent_checker_pass` 一个 acceptance，并以字符串 marker 判 PASS | 无法证明主人的每项验收要求；失败不能形成可执行的最小修复包 | 已冻结“主人指令/相关检查”两项通用合同；Checker 严格 JSON 返回，每项单独 Evidence；CHANGES_REQUIRED 含 acceptance_id、实际/预期、允许范围和重验命令。待 Planner 拆分自然语言细粒度验收 | 部分解决 | `test_checker_changes_required_is_a_bounded_repair_package`；代码任务集成测试；20 项回归通过 |
| I-20260723-19 | 2026-07-23 | 任意非写入/探针指令直接视为中型代码任务；大型 Plan 只能由主人手工提交 JSON | Butler/Planner 无法按真实复杂度自动形成最小方案和 DAG | P1：实现最小可解释分类；大型任务生成至少两套方案并在高置信度时自动选择，否则项目管家一次只问一个问题 | 待处理 | `normalize_instruction` 默认 `provider_task`；`planner.py` 只有 `normalize_plan` |
| I-20260723-20 | 2026-07-23 | 原 retry 直接重跑，没有先对账 HostJob/Session/workspace/Evidence/handoff；超时只传给同步 Bridge | 中断或返回不明确时可能重复执行，尤其不能保护不可逆动作 | 执行器按稳定 job_id 有界对账；模糊派发只按冻结 retry/backoff 重试，耗尽后保持活动 HostJob 并以 unsafe_unknown 停止，禁止改 TaskSpec，只允许继续对账或取消。待补完整 deadline/stop_grace/handoff 与 Checker 恢复 | 部分解决 | `test_ambiguous_dispatch_stops_for_decision_without_blind_replay`；21 项回归通过 |
| I-20260723-21 | 2026-07-23 | 只有 Warm `current.json` handoff 与归档 revision | 长期任务缺少 Hot Capsule、Cold 分段、轮转和 Provider compact 协调 | P1：复用有界文件原语补齐三层语义；Context/rotation/checkpoint 上限按来源冻结并执行 | 待处理 | `continuity.py` 只有 `checkpoint_project` |
| I-20260723-22 | 2026-07-23 | 不可逆权限仅靠固定 prompt 和少量确认，未保存绑定范围的授权事实 | Session 替换后不能安全继承授权，也不能证明授权已在终态失效 | P1：在不新增权威表前先用 messages 结构化 action 表达临时授权，并按 project/task/revision/scope/expiry 验证与失效 | 待处理 | 无 authorization 数据结构或校验器 |
| I-20260723-23 | 2026-07-23 | 设置与资源库只有只读页；Project 规则、模板/脚本晋升和多项阈值没有执行路径 | TaskSession 快照看似完整但不能形成可维护的业务配置闭环 | P1：只开放基线列明的有限 action；校验、版本化、冻结来源，并只接入首个真实消费者 | 待处理 | `/api/settings-library` 只有 GET |
| I-20260723-24 | 2026-07-23 | 没有一致性备份、候选隔离、生产调度租约切换和回滚工具 | 无法满足 §24 蓝绿切换门禁；当前 8750 只能算本地候选演示 | P2：真实任务闭环稳定后再实现，任何切流必须主人单独批准 | 待处理 | Docker 只有单镜像构建与手工回滚容器 |
| I-20260723-25 | 2026-07-23 | 曾用“台账显式待处理为 0”和“7/7 Task Done”宣称 V1 完成 | 混淆局部运行结果与冻结基线覆盖，导致遗漏未被记录 | 永久使用本章逐项覆盖审计；部分实现不得计完成；每次报告同时列出未做项 | 已解决 | 本次 26 章覆盖矩阵和 I-20260723-16 至 24 |
