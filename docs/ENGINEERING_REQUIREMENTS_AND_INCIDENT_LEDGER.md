# PlowWhip Web 工程需求与故障账本

> 本文是结构化项目台账的自动生成人类视图；禁止直接编辑。完整真源位于 `docs/engineering-ledger/`。
>
> **ledger revision：** `2026-07-21.5`  
> **模型视图：** [`ENGINEERING_MODEL_LEDGER.md`](ENGINEERING_MODEL_LEDGER.md)。两份视图由同一真源一次生成，不再手工同步。

## 1. 强制使用规则

以下任一变更都属于“重大改造”，开始前必须完整阅读本文相关条目：

- Goal、Task、attempt、execution episode、Verification 等领域状态或终态语义；
- 项目管家、全局管家、Planner、角色拆分、Worker/Provider/SessionBinding；
- 调度、并发、租约、幂等、重试、断点续接、Watchdog、熔断或恢复；
- EvidenceManifest、产物、验收、完成判定、终态重写或删除；
- Token 计量、Context 续接、缓存归因或预算；
- 数据库迁移、Host Bridge、安全边界、部署和回滚；
- 影响确认、派发或恢复的前端主流程；
- 跨前后端、跨多个领域模块或会改变已有运行数据解释方式的改造。

### 改造前

1. 读取本文中相关需求、事故和开放风险。
2. 记录当前分支、HEAD、dirty worktree、数据库 schema head、部署 revision/image；不得把它们混为一个“版本”。
3. 列出受影响的状态组合、失败路径、恢复路径和与旧机制的冲突；不能只验证成功路径。
4. 明确目标、边界、验收标准、兼容策略和最小回滚/恢复动作。
5. 先检查活动 Host Job、活动模型调用和迁移兼容性；存在活动执行时，不得重建、重启或部署控制面。
6. 优先删除、合并或统一冲突机制，不为每个故障继续叠特殊分支。

### 改造后

1. 更新需求状态，并新增或更新故障条目。
2. 写清故障现象、已验证原因、处理方式、处理结果、关联风险和防回归验收。
3. 附上可复查的代码、测试、API、数据库、Git 或部署证据；模型声明、heartbeat、queued、accepted、HTTP 200 或单独的 exit code 0 都不能证明完成。
4. 分别声明：本地代码、已提交代码、远端分支、部署镜像和运行数据库的状态。
5. 未做真实回归时只能标记“已处理待验证”，不得标记“已闭环”。

如果本文缺失、无法读取或与当前代码明显矛盾，重大改造必须停止，先恢复账本或完成事实核对。

## 2. 状态定义

| 状态 | 含义 |
| --- | --- |
| `USER_REPORTED` | 用户已报告现象或需求，尚未用当前代码/运行状态完整验证。 |
| `VERIFIED_OPEN` | 现象和证据已复现或核实，根因/修复/验收仍未闭环。 |
| `MITIGATED_UNVERIFIED` | 已修改代码或运行状态，但缺少足够回归或真实链路证据。 |
| `VERIFIED_CLOSED` | 根因、修复、回归和当前生效范围均有证据。 |
| `REQUIREMENT_OPEN` | 已确认的产品/工程合同，尚未证明完整实现。 |
| `SUPERSEDED` | 被明确的新机制替代；必须指向替代条目，不能静默删除历史。 |

“关闭”只描述条目中明确列出的范围，不代表整个无人值守系统已经可用。

## 3. 不可裁剪的产品与工程合同

### R-001 质量优先的无人值守主线

- **状态：** `REQUIREMENT_OPEN`
- 用户只提供自然语言指令；项目管家应自行理解、澄清、拆分、选角色/规则/Provider、绑定 Task Session、派发、监控、恢复、验收和交付。
- 主流程不得依赖外部 Codex Goal，也不得要求用户知道内部 Task、角色、报告相对路径或恢复按钮背后的状态细节。
- 不能靠持续增加补丁、特殊分支、人工搬运状态和伪终态实现“看似完成”。
- **最终验收：** 至少一次全新项目从自然语言指令到声明产物和独立验证完整结束，全程无人工改代码、改数据库或手工推进状态；随后分别经历一次受控 Provider 中断和一次控制面重启，均从结构化断点有界续接并保持证据一致。

### R-002 全局管家与项目管家

- **状态：** `REQUIREMENT_OPEN`
- 全局管家用于查看工作区内所有项目的规范化状态并路由到隔离的项目管家，不替项目管家执行项目规划。
- 每个项目创建时生成一个项目管家。项目管家默认接入 Codex CLI 作为智能规划模型，不能用固定问卷或字符串规则冒充智能理解。
- 大型目标一次只问一个最有价值的问题，直到目标、边界、验收标准达到语义可信度阈值，再交主人确认；中小目标可在合同完整时自主规划。
- 模型不可用时必须 fail closed，保留原始输入并进入可恢复状态；禁止机械生成目标、边界、验收或任务。

### R-003 角色、规则、模板与会话

- **状态：** `REQUIREMENT_OPEN`
- 角色库、规则库和模板库应持久化；项目管家根据任务选择或生成角色实例并绑定 Task Session。
- 有模板则复用；无模板可创建并进入模板库。项目规则与模板冲突时，仅在项目实例上叠加项目规则，不污染公共模板。
- “编码前思考、简洁优先、精准修改、目标驱动执行”对开发类角色不可随意裁剪；`simple-worker` 仅执行确定性本地命令，可不使用智能角色实例。
- 物理 Provider Session 身份严格为 `project_id + role_id + task_id`；同 Task 重试才可续接，同 project+role 只保留逻辑 Worker 和责任边界。

### R-004 断点续接而非推倒重来

- **状态：** `REQUIREMENT_OPEN`
- 重试第一步应读取同一 Task 的结构化 checkpoint、已验证 artifact、Git/工作区增量、上次故障和最小待办，再做最小检测。
- 同 Task、同 `session_generation` 不因 `run_id` 变化强迫无意义改写；新 generation 只接收有界结构化 handoff，不重放旧聊天或完整日志。
- Planner、创建、派发和重试必须使用稳定幂等键，重复调用不得生成重复 Goal/Task/物理进程。

### R-005 网络、Provider、熔断和恢复

- **状态：** `REQUIREMENT_OPEN`
- 全局断网：暂停所有进行中任务并归并为一个网络根因告警；恢复后按原 Task 断点有界续接。
- 单 Provider 不可用：独立探测三次后，在满足任务约束时切换 Provider；耗尽恢复预算后进入 `network_suspended`、`provider_suspended` 或 `needs_human`，不能伪装成业务验证失败。
- DeepSeek/Kimi 属于中国大陆有网即可用的 domestic 路径；ChatGPT/Cursor 依赖 overseas/proxy 路径。路由必须区分“全局断网、国内网络可用但海外链路不可用、单 Provider 不可用”。
- 人可以批准恢复，但批准必须续接原 Task 的结构化状态，不得创建无边界的新执行。
- 告警按根因去重、抑制和收敛；全局断网时不为每个 Task/Provider 重复报警。

### R-006 Watchdog 与有界执行

- **状态：** `REQUIREMENT_OPEN`
- 禁止一个全局硬编码 900 秒作为所有 Episode 的固定上限，也禁止只换成另一个更大常量。
- 有效 wall/observation/checkpoint/no-progress/host-process 限制按“直接 Task+role convention > project setting > global setting”解析并记录来源。
- 有可核验证据进展时允许有界续期，但不得超过 Task hard deadline；heartbeat 或输出字节本身不算进展。
- 保留 Token burn-rate、同错、零进展、最大物理进程和 circuit-open 保护。

### R-007 Evidence、产物与完成判定

- **状态：** `REQUIREMENT_OPEN`
- 管家必须根据目标自动分配安全、可读的项目相对产物路径，并在确认方案和任务详情中显示；用户不需要知道内部目录深度。
- EvidenceManifest 必须绑定真实命令、精确退出码、Host Job/run/session、验收项及产物路径/哈希；空命令、泛化 exit code 和模型自述不能完成任务。
- 验证结论只能是结构化 `PASS` 或 `CHANGES_REQUIRED`；后者禁止生成 `passed=true`。
- 报告型验收没有声明产物时必须拒绝；目标不能在 required artifact 缺失时进入 completed。
- 经授权的自动化可以重写终态和证据历史，但必须保留 revision、操作者、原因和新证据，不得静默覆盖。

### R-008 Token 计量与看板

- **状态：** `REQUIREMENT_OPEN`
- 前期不以 Token 预算阻塞执行，但必须保留原始 Provider 快照，并按物理 Session 增量归一化，明确花销归属到项目、Goal、Task、角色、Provider 和日期。
- `cached_input_tokens` 是 input 的子集，不能与 input 再次相加。
- 手动刷新和自动刷新必须在同一刷新代次内同时更新 Goal/Task、Worker/Host Job、SessionBinding、artifact/evidence、verification 和 Token；项目范围、时间范围或刷新代次变化后，旧请求不得覆盖新数据。
- 运行中的模型调用尚未返回 Token 快照时，界面必须显示“Provider 尚未结算”或等价未知态，不能用 `0 Token` 冒充已确认的零消耗；Provider 提供中间快照时可显示明确标记的暂估值，完成后再以 ModelCallLedger 归一化结算值替换。
- 日线图使用前一日结束时的历史分布计算当日颜色档位，零点更新；项目范围和全局范围必须使用对应数据。
- 大数必须完整可读或使用明确单位/tooltip，不能用 CSS 省略号隐藏关键数值。

### R-009 UI 主流程与可观察性

- **状态：** `REQUIREMENT_OPEN`
- 管家是可上翻历史的对话栏；全局范围只显示全局管家入口，项目范围只显示项目管家入口。
- “项目”排在“任务”前；任务与任务看板合并；切换项目时列表、详情、今日 Token 和趋势数据必须同时切换，不能保留旧选择。
- `candidate_ready` 必须显示为“候选完成/待下游或独立验证”，不能归入“进行中”；依赖已经解除、Provider 仍在执行、等待证据结算和最终完成必须使用不同文案。
- 点开运行中 Task 时，必须显示当前绑定的 Provider/模型、Task Session、Host Job、最近心跳、最近可核进展和经过脱敏的有界实时输出。模型可见输出至少区分 assistant 回复、tool/status 和错误；使用 cursor 增量续读，不重放完整历史，也不暴露内部隐式推理、密钥或未脱敏原始日志。
- 所有终态/历史列表必须按 canonical 终态时间倒序，最近结束的项目、Goal 或 Task 位于最上方；相同时间使用稳定 ID 作为最终排序键。`candidate_ready` 不是终态，必须单独分组并按进入候选态的事件时间倒序。筛选、分页和自动刷新不能破坏顺序或把旧条目重新插到顶部。
- 一级菜单的项目范围标签不得折行；终态泳道内容不得越界。
- 设置页只展示常用、安全的策略，继承值和来源可见；低频高风险项放入高级设置，不把状态机细节推给普通用户。
- 自动刷新支持 5s、10s、30s、1min、5min、10min、1h、2h、4h，并显示最后成功刷新时间和失败状态。

### R-010 删除、运维与安全

- **状态：** `REQUIREMENT_OPEN`
- 提供 Goal 和 Task 删除接口，但仅允许满足删除条件的终态对象；删除影响、关联证据和审计记录必须明确。
- Host Bridge 只允许注册项目根目录内的受控操作，路径必须规范化并防逃逸；密钥不得进入日志、报告或诊断包。
- 部署、提交、远端 main、tag/release 和 8742 当前运行镜像分别验收，不能用其中一个代替其他状态。

### R-011 可复现的空库冷启动

- **状态：** `MITIGATED_UNVERIFIED`
- GitHub 和发布镜像不得携带用户 SQLite 数据库、密钥、项目、对话、Token 或证据历史。
- schema 必须由 Git 跟踪并随 wheel/镜像打包的有序 SQL migration 创建；默认 Provider、管家身份、规则库、角色模板和全局 Convention 必须由版本化源码幂等初始化。
- 空库启动应得到完整可用 catalog；已有数据库和用户修改的 Convention 不得被默认种子覆盖。
- **本地证据：** `pyproject.toml` 打包 `store/migrations/*.sql` 与 `defaults/*.md`；`create_app()` 先迁移再幂等初始化；`tests/test_fresh_install_bootstrap.py` 覆盖空库和重启保留。2026-07-21 本地 wheel 检查包含 34 个 migration SQL 和默认 Convention，wheel 冷启动通过。
- **未闭环项：** GitHub `main` 当前 SHA `05173e3983abc42776bbdd197411bccb72ddc2b2` 只有 migration 0001–0033；本轮默认 Convention、0034、账本和冷启动测试尚未提交远端。

### R-012 任务依赖图、顺序与并发资格

- **状态：** `REQUIREMENT_OPEN`
- 项目管家拆分复杂目标时必须输出并持久化 Task DAG；每个 Task 使用稳定标识声明 `depends_on`、所需上游 artifact/evidence 和交付给下游的 artifact，不得依赖标题、数组位置或 UI 排序推断前后置关系。
- 计划入库前必须执行确定性 preflight：拒绝自依赖、环、缺失节点、跨 Goal 非法依赖、不可满足的 Provider/角色/产物约束；保存通过校验的 GoalSpec/TaskSpec revision 和拓扑摘要。
- 调度资格必须由一个统一谓词计算：全部直接上游达到允许交付的状态、结构化 verdict 为 `PASS`、required artifact 存在且哈希匹配、TaskSpec revision 未失效、所需资源无冲突。只有状态名或 heartbeat 不能解锁下游。
- 实现 Task 可用 `candidate_ready + PASS EvidenceManifest + artifact contract passed` 解锁实现型下游；最终 Goal 完成仍必须经过全新独立 verification Task。`CHANGES_REQUIRED`、证据矛盾、artifact 缺失、`network_suspended`、`provider_suspended`、`needs_human`、取消或真实失败都不得错误解锁。
- 无依赖路径的 Task 才有并行资格；即使 DAG 独立，共享独占工作区、数据库迁移、部署环境、同一受限资源或不兼容 Provider Session 时仍需通过资源锁串行化。
- 上游重试在同一 TaskSpec revision 内保留下游阻塞关系；amend/replan 产生新 revision 后必须重算受影响子图。若下游尚未开始则使其失效并重新等待；若已经开始则进入有原因、可审计的暂停/取消/重新验证流程，禁止静默修改依赖后继续。
- UI 必须展示当前 Task 的直接上游、直接下游、阻塞原因、已满足/未满足的依赖条件和当前 plan revision；“候选完成”与“整个 Goal 已完成”不得混淆。
- **组合验收矩阵：** 至少覆盖线性 `A→B→C`、扇出 `A→{B,C}`、汇合 `{B,C}→D`、合法并行、环、缺失依赖、自依赖、上游重试、`CHANGES_REQUIRED`、artifact 缺失、Provider/网络挂起、取消、attempt 耗尽、控制面重启、同 revision 续接、方案修订时下游未启动/已启动、共享资源冲突和最终独立验证；重复 scheduler tick 不得重复解锁、重复创建 Task 或重复启动物理进程。

### R-013 按项目隔离的工程需求与故障台账

- **状态：** `REQUIREMENT_OPEN`
- 全局 Convention 只定义台账的强制使用规则、字段模板、读取时机和最低验收要求，不保存各项目的具体需求、事故正文或处理历史。
- 每个注册项目拥有且只拥有自己的 canonical 台账，按 `project_id` 隔离；默认落在该项目产物源目录内的约定路径，并在项目元数据中登记路径和 revision。PlowWhip Web 控制面自身也按普通项目管理，其台账就是本文。
- 创建或注册项目时，如果项目内尚无台账，只从版本化模板初始化空结构、项目身份和规则说明；不得复制其他项目的需求、事故、路径、人员信息或历史结论。
- 项目管家只能读取和更新本项目台账。全局管家可以查询所有项目的台账索引、开放风险摘要、revision 和更新时间；查看正文时必须明确指定项目，不建立混合正文真源。
- Task/角色实例只获得当前项目、当前目标相关的条目 ID、revision 和有界摘要；需要时再按引用读取正文，禁止把所有项目台账注入 Context。
- 跨项目共性规则经人确认后提升到全局 Convention 或版本化模板；原项目事故仍保留在原台账，并记录提升后的规则 ID，不能通过移动或复制事故正文冒充全局事实。
- 项目删除、移动、重新注册、克隆和同目录多工作树必须有明确归属策略：不能仅用可变路径判定台账身份，也不能让两个 `project_id` 静默共享一份可写台账。
- **验收：** 注册两个项目后分别写入互不相同的需求和事故；项目管家只能看到本项目内容，全局管家能看到两个索引但不会混合正文；角色 Context 不含另一项目条目；项目路径移动后仍绑定正确台账；全局模板升级不覆盖任何项目已有内容。

## 4. 故障账本

### I-001 项目注册报错、路径语义不清

- **日期/来源/状态：** 2026-07-20，用户截图；`USER_REPORTED`
- **故障现象：** 注册项目时页面显示 `[object Object]` 或 `HTTP 500`；“控制面挂载路径”和“本机项目目录”被填成相同本机绝对路径；中文项目名/目录组合曾无法提交。
- **影响：** 用户无法判断输入合同，注册失败原因不可操作；错误对象被错误字符串化。
- **已验证证据：** 用户截图显示上述错误和表单值；本条尚未重新通过当前 API 复现。
- **故障原因：** 待当前版本复现后确认。候选风险包括前端错误归一化缺失、控制面逻辑路径与 host project path 校验不一致、重复项目/路径冲突没有映射为可读 4xx。
- **处理方式：** 历史会话中曾调整文案和错误展示，当前分支仍需回归。
- **处理结果：** 未证明闭环。
- **关联风险点：** Unicode/空格/下划线、重复注册、软链接、目录不存在、目录已被其他项目占用、容器逻辑路径与 Host Bridge 根目录混淆。
- **验收：** 覆盖有效中文路径、重复名称、重复本机路径、非法挂载路径、目录不存在；每种失败返回结构化 4xx 和可执行中文说明，前端不得出现 `[object Object]`。

### I-002 项目管家机械问卷、模型不可用后不可恢复

- **日期/来源/状态：** 2026-07-20，用户截图与多轮实际操作；`MITIGATED_UNVERIFIED`
- **故障现象：** 管家按固定字段机械追问，无法理解用户已经给出的目标；Codex CLI 调用失败后会话挂起，恢复按钮无效，新会话返回后仍显示旧会话无模型结果。
- **影响：** 无法智能拆分角色/规则/任务；用户变成传声筒，主流程中断。
- **已验证证据：** `backend/plow_whip_web/runtime/butler.py` 当前存在模型调用账本和 fail-closed 路径；`backend/plow_whip_web/store/butler_repository.py` 明确拒绝无模型结果时机械降级；相关测试位于 `tests/test_butler_intake.py`、`tests/test_butler_planner.py` 和 `web/src/ProjectButlerDialog.test.tsx`。这只能证明当前候选实现，不证明完整生产链路。
- **故障原因：** 原实现把结构完整度问卷当成语义规划器；Provider 失败、会话恢复和 UI 持久状态没有形成同一个 canonical 状态机。
- **处理方式：** 当前 dirty 分支已接入 Codex Planner、fail closed、持久化 planning 状态和恢复显示。
- **处理结果：** 单元/前端回归已通过，但尚无一次全新自然语言指令到最终产物的真实闭环，因此不能关闭。
- **关联风险点：** Provider 探测与真实调用能力不一致、同步 HTTP 长调用、控制面重启、重复点击、新会话选择、旧 conversation revision、模型响应格式错误。
- **验收：** 真实执行大型和中小型目标各一次；大型目标识别已给信息并只问缺失项；Provider 中断后可恢复且不重复创建 Goal/Task；无模型结果时绝不机械填充。

### I-003 Task 长期停在 verifying、零 Token 或无证据仍显示进展

- **日期/来源/状态：** 2026-07-19 至 2026-07-20，用户截图与运行监控；`VERIFIED_OPEN`
- **故障现象：** Task 卡在“验证中”，Token 为 0，任务详情缺少产物；部分 Goal/Task 状态与真实执行、终态或依赖不一致。
- **影响：** 系统不能无人值守收敛；状态看似在运行但没有证据进展。
- **已验证证据：** 历史 Goal 监控记录出现重复 attempt、paused/terminal 不一致、SessionBinding 和 Verification kind 不收敛；当前代码已有 `EvidenceManifest` 一致性校验，但尚未证明所有旧状态组合可自愈。
- **故障原因：** 完成判定、验证 verdict、依赖解锁、重试和父 Goal reducer 曾由多套机制分别修改状态，缺少唯一 reducer 和终态不变量。
- **处理方式：** 当前分支加强 Verification/EvidenceManifest 约束、Goal reducer 和恢复逻辑。
- **处理结果：** 代码级测试通过不等于运行链路关闭；本条保持开放。
- **关联风险点：** `CHANGES_REQUIRED` 被写为 passed、verification Task 被标 implementation、父 Goal completed 后遗留 paused child、Host Job 完成但证据未落库、重复 verifier。
- **验收：** 枚举每个 child 状态和 verdict 组合的 reducer 测试；真实链路中 verifying 必须在有界时间进入 completed、retryable、suspended、needs_human 或 terminal_failed，且每个转移有结构化原因。

### I-004 报告型任务可在零产物、空命令或泛化退出码下完成

- **日期/来源/状态：** 2026-07-20，任务详情与代码审查；`MITIGATED_UNVERIFIED`
- **故障现象：** “独立验证证据并汇总优先级报告”显示 completed，但任务产物为 0；证据曾只依赖空命令或泛化 exit code。
- **影响：** 没有报告却声称完成，破坏整个完成判定的可信度。
- **已验证证据：** 当前 `backend/plow_whip_web/store/task_repository.py` 校验 exact command/run/session、artifact hash、acceptance id 和 canonical verdict；`backend/plow_whip_web/runtime/orchestration.py` 拒绝报告型验收无产物；测试覆盖 `tests/test_evidence_manifest.py`、`tests/test_orchestration.py`、`tests/test_host_job_continuity.py`。
- **故障原因：** 旧 artifact contract 允许空集合真值，EvidenceManifest 未强制绑定物理执行和验收项。
- **处理方式：** 当前 dirty 分支加入非空报告产物合同、精确命令证据、产物哈希和 verdict 一致性。
- **处理结果：** 后端全量测试和负向 API 校验曾通过，且已部署到 8742；但代码未提交，尚缺全新真实审查任务的最终报告证据。
- **关联风险点：** 预存旧文件冒充本轮产物、跨 run 继承、相对路径逃逸、验证任务写代码、前端仅展示 declared 而非 exists。
- **验收：** 缺产物、旧产物、错误哈希、空命令、错误 session/run、`CHANGES_REQUIRED + passed=true` 均必须失败；真实报告任务必须生成并在 UI 可定位的文件。

### I-005 用户被迫指定深层报告路径

- **日期/来源/状态：** 2026-07-20，用户反馈；`MITIGATED_UNVERIFIED`
- **故障现象：** 用户不知道审查报告应该填写哪个相对路径，担心不指定就不会生成文件。
- **影响：** 内部执行细节泄漏到产品入口，破坏自然语言无人值守体验。
- **已验证证据：** `backend/plow_whip_web/runtime/orchestration.py` 当前可为报告型任务生成相对 artifact；`web/src/ProjectButlerDialog.test.tsx` 断言方案展示 `reports/engineering-audit-<id>.md`；任务 UI 可显示 host path、复制并在 Finder/Cursor 打开。
- **故障原因：** 旧流程把 artifact contract 的创建责任放给用户，Planner 没有把交付物类型映射为安全路径。
- **处理方式：** 当前 dirty 分支由管家自动分配项目相对路径并在确认方案中展示。
- **处理结果：** 单元/UI 测试已过，尚需真实 Goal 证明 Worker 实际写入同一路径。
- **关联风险点：** 多报告重名、重试覆盖、Unicode 文件名、路径逃逸、项目根目录映射、声明路径与 Worker 提示不一致。
- **验收：** 用户只说“输出报告”即可；系统生成稳定相对路径，Worker 写入，EvidenceManifest 哈希匹配，UI 能定位，重试不另造无关路径。

### I-006 部署重启中断正在进行的管家模型调用

- **日期/来源/状态：** 2026-07-21；`MITIGATED_UNVERIFIED`
- **故障现象：** 用户于 17:21:15 UTC 提交管家指令；17:21:56 控制面被重启，模型调用 `b5bc741b...` 永久停在 `dispatched`；第二次调用 `add4facc...` 于 17:23:07 完成。
- **影响：** 会话看似卡死、Token 可能已消耗却无结果，用户需要重试。
- **已验证原因：** 本次部署在模型调用进行中重启控制面；生命周期工具此前只检查 Host Job，没有把 prepared/dispatched model call 当作活动执行。
- **处理方式：** `scripts/plow_whip_ops.py` 现在同时检查活动 Host Job 和活动 model call；启动恢复在 `backend/plow_whip_web/runtime/recovery.py` 将上个进程遗留调用标为 `unknown/control_plane_restarted`；前端显示持久 planning 状态并在请求失败后重载 conversation。
- **处理结果：** 后端全量测试、前端 27 项测试/typecheck/lint/build 已通过；8742 健康，遗留调用已归类 unknown，当前活动 Job/模型调用为 0。修改仍在 dirty worktree，未提交/未推送。
- **关联风险点：** 强制 override、容器被外部工具重启、模型调用无心跳、长连接断开但 Provider 仍运行、重复提交、账本结算和 Token 快照丢失。
- **验收：** 活动模型调用时部署命令必须拒绝；受控 kill 后调用进入 unknown、会话可恢复且不会伪装 completed；重试使用稳定幂等身份。

### I-007 Token 大数显示被省略

- **日期/来源/状态：** 2026-07-20，用户截图；`USER_REPORTED`
- **故障现象：** 数千万 Token 未到一亿时，卡片只显示 `3...`、`283,1...`、`51,658,...`。
- **影响：** 关键成本数据不可读，看板失去核账用途。
- **已验证证据：** 用户截图；历史会话曾声明修复并发布 v2.0.2，但本条未对当前浏览器宽度和现行部署重新回归。
- **故障原因：** 待复验；高概率为固定卡片宽度、`text-overflow: ellipsis` 和非响应式数字排版共同造成。
- **处理方式/结果：** 历史已有候选修改，当前状态不得假定已闭环。
- **关联风险点：** 窄屏、浏览器缩放、中文标签、九位以上数字、单位缩写误解、tooltip 缺失。
- **验收：** 1、999,999、51,658,000、999,999,999、十亿级在支持宽度下完整可读；窄屏采用明确单位并提供原始值 tooltip，不得只显示无意义省略号。

### I-008 Token 归因、趋势和项目范围不一致

- **日期/来源/状态：** 2026-07-19 至 2026-07-20，用户反馈；`VERIFIED_OPEN`
- **故障现象：** Input 远高于 Output，项目切换后“今日 Token”不变，折线未区分项目与全局；用户怀疑重复计量和 Context 浪费。
- **影响：** 无法定位真实 Token 消耗，优化是否有效无法判断。
- **已验证证据：** 当前 `backend/plow_whip_web/runtime/token_ledger.py` 和已有迁移实现 raw snapshot/normalized delta 方向；但历史数据含 legacy inferred delta，当前 UI/数据集全链路仍需审计。
- **故障原因：** 需要分别验证正常输入成本、缓存输入子集、Provider 累计快照重复相加、物理 Session 跨任务污染和前端 scope 缓存。不能仅凭 Input/Output 比例判定浪费。
- **处理方式/结果：** 已有部分实现和测试，不足以关闭。
- **关联风险点：** cached input 重复加总、重试重复快照、SessionBinding 漂移、日期时区、项目筛选、零点基准、图表缓存。
- **验收：** 用可控模型调用构造 raw snapshots，逐次核对归一化增量；项目/global/API/UI 数值一致；Asia/Shanghai 日界正确；列出正常成本与有证据的浪费来源。

### I-009 前端范围、导航、布局和自动刷新问题组

- **日期/来源/状态：** 2026-07-19，用户列出 14 项并提供多张截图；`USER_REPORTED`
- **故障现象：** 管家栏堆叠、范围切换仍显示两个管家、缺少全局管家对话入口、项目范围折行、所有菜单错误显示项目管家入口、任务详情不随项目切换且终态仍 running、项目/任务菜单顺序不合理、任务与看板割裂、Token total 数字排版低质、趋势 scope/颜色/点值错误、设置复杂固定、缺少指定档位自动刷新、长方案和泳道内容越界。
- **影响：** UI 展示与 canonical 状态脱节，操作入口误导，长内容不可用。
- **已验证证据：** 用户截图证明部分溢出和旧状态；当前分支含大量 UI 修改，但尚未逐项用现行 8742、不同宽度和项目数据做验收矩阵。
- **故障原因：** 多页面复制 scope/header 逻辑、selected detail 未在 filter 改变时失效、固定栅格/nowrap/ellipsis 混用、设置模型直接映射内部配置。
- **处理方式/结果：** 历史有多轮候选修复，整体状态保持未闭环。
- **关联风险点：** 空项目、删除项目、深链接、自动刷新并发、旧请求覆盖新 scope、移动宽度、超长中文、Goal/Task 终态竞态。
- **验收：** 将 14 项拆成可自动化的 UI 测试矩阵，并在 8742 做浏览器验收；每次 scope 切换同时校验 header、入口、列表、详情、Token、趋势和请求参数。

### I-010 Watchdog 固定上限、重试推倒重来和 Planner 非幂等

- **日期/来源/状态：** 2026-07-20，用户质疑与历史运行证据；`VERIFIED_OPEN`
- **故障现象：** 长任务被固定 900 秒误杀；重试重新读取/执行大量旧工作；跨 run artifact 规则迫使 no-op 改写；Planner 重试可能重复生成。
- **影响：** 浪费 Token、破坏已完成工作、产生重复任务或物理进程，最终触发错误熔断。
- **已验证证据：** 历史 attempt 记录和代码审查确认旧固定上限及跨 run artifact 冲突；当前分支包含 fault policy、continuity、model call ledger 修改和测试，但尚未完成真实 XL 任务验收。
- **故障原因：** Episode 预算、Task hard deadline、Provider Session、artifact provenance、Planner 幂等分别演进，缺少统一 execution episode 合同。
- **处理方式/结果：** 当前候选代码部分统一限制和续接语义，保持开放。
- **关联风险点：** 相同错误无限重试、checkpoint 过大、旧聊天重放、物理进程泄漏、并行 verifier、run_id 与 task identity 混淆。
- **验收：** XL 4800 秒任务不在 900 秒被杀；无进展任务仍有界终止；配置继承来源可见；续期不超过 hard deadline；同 Task 保留 artifact/session continuity；重复 Planner 调用只产生一个计划。

### I-011 网络/Provider 故障被伪装成业务失败，恢复和告警不收敛

- **日期/来源/状态：** 2026-07-20，用户要求与断网实际经历；`REQUIREMENT_OPEN`
- **故障现象：** 断网或单 Provider 失败时 Task 熔断/挂起，用户无法判断是否自动恢复；每个任务可能重复报告同一根因。
- **影响：** 基础设施故障污染业务验收，人工介入增加，恢复后可能推倒重来。
- **已验证证据：** 当前 provider pool/fault policy 有部分探测、暂停和恢复实现；尚未完成“国内网络、海外链路、单 Provider、全局断网”四类故障注入矩阵。
- **故障原因：** 网络域健康、Provider 健康、Task 执行状态和告警聚合未由同一故障分类合同驱动。
- **处理方式/结果：** 需求已进入当前候选设计，未证明闭环。
- **关联风险点：** DNS 可用但 API 不可用、代理失效、认证失败误判网络、限流误切换、不同 Provider 能力不等价、恢复风暴、重复告警。
- **验收：** 故障注入覆盖四类网络域；状态进入正确 suspended/needs_human 而非 verification failed；三次探测和切换可审计；恢复只续接一次；同根因只保留聚合告警及受影响对象数量。

### I-012 全局规则只存在于当前机器 SQLite

- **日期/来源/状态：** 2026-07-21，空库冷启动核查；`MITIGATED_UNVERIFIED`
- **故障现象：** 34 个迁移、7 个规则、7 个角色模板和 Provider catalog 可从源码生成，但全新临时数据库的 global Convention 为 revision 0、`present=false`；刚加入的重大改造账本门禁只存在于 8742 当前数据卷。
- **影响：** 其他人克隆或从空数据卷启动时会丢失全局连续性规则，与当前机器行为不一致。
- **已验证原因：** `ConventionRepository` 原来只有 get/put，没有空库默认种子；全局 Convention 是通过运行 API 手工写入。
- **处理方式：** 新增随 wheel/Docker 打包的 `backend/plow_whip_web/defaults/global_convention.md`；启动迁移完成后仅在 global 行不存在时幂等写入 revision 1，现有行无论内容和 revision 如何均不覆盖。
- **处理结果：** 当前本地候选空库可由同一源码得到迁移、Provider、全局管家、规则/模板和 global Convention；用户修改后重启保持 revision 2 和原内容。GitHub `main` 尚未包含本轮候选，不能声明外部克隆已获得修复。
- **关联风险点：** wheel 漏打包 package data、默认种子覆盖用户配置、提交真实数据库、默认模板与在线 revision 漂移。
- **验收证据：** 本地 `tests/test_fresh_install_bootstrap.py`、后端全量 `262 passed`；wheel 内容检查包含 34 个 migration SQL 和 `defaults/global_convention.md`。完成远端提交后还需核对 GitHub tree 和 CI，才能改为 `VERIFIED_CLOSED`。

### I-013 候选完成显示为进行中、运行 Token 显示 0、Task 详情缺少模型实时返回

- **日期/来源/状态：** 2026-07-21，8742 真实审核 Goal `1b052b09-8f37-4b3b-937d-fb225c039a43`；`VERIFIED_OPEN`
- **故障现象：** 前端和后端审核 Task 已生成报告并通过候选证据门后，页面仍让用户理解为“进行中”；运行期间 Token 显示 0；用户点开 Task 看不到当前模型正在返回什么，因而误判任务僵死并担心后置任务被阻塞。
- **影响：** canonical 执行正常时 UI 仍制造错误告警；用户无法区分“模型仍运行、等待结算、候选完成、依赖已解锁”，容易手工重试或中断真实活动任务，造成重复执行和 Token 浪费。
- **已验证证据：** 后端 Task `1e6070ee-f048-4d7d-96f9-ae9b6d9beb20` 于 02:17:08 进入 `candidate_ready`，Host Job `returncode=0`，最终结算 `2,298,552` Token；此前 `/api/usage` 已记录 4 次调用但 Token 字段均为 0。其下游 Task `146cffbf-fe5f-4afe-a7d2-76606cc627cd` 于 02:18:08 解锁、02:19:14 进入 `running`，证明本次线性依赖没有实际阻塞。`/api/workers/{worker_id}/stream` 已能用 cursor 提供有界输出，但主 Task 详情未把这一能力清晰呈现。
- **已验证原因：** `candidate_ready` 的 UI 语义/泳道与用户理解不一致；Token 只在 Provider/Host Job 完成后结算，未结算态被展示为数值 0；Task 详情没有把 Worker stream、结算状态和依赖资格组合成同一观察面。
- **处理方式/结果：** 本次按用户要求只记录，不修改代码、不重试任务、不干预当前 Goal。
- **关联风险点：** 自动刷新只更新部分 API、旧 scope 请求覆盖新 scope、Provider 不提供中间 usage、Token 暂估与最终值跳变、输出流泄密或无界、SSE 断线、candidate_ready 被错误当成 Goal completed、UI 手工重试造成重复 Host Job。
- **验收：** 一次刷新原子更新状态与 Token 观察面；未结算调用显示未知/待结算而不是 0；运行中 Task 可看到脱敏有界的最新 assistant/tool/status 输出及 cursor；`candidate_ready` 使用独立标签；线性和扇出/汇合依赖在 UI 与 API 中同时显示已满足/未满足条件，且后置 Task 的实际调度与展示一致。

### I-014 终态/历史列表未按最近完成时间倒序

- **日期/来源/状态：** 2026-07-21，用户在 8742 查找刚生成的稳健性报告时反馈；`USER_REPORTED`
- **故障现象：** 终态或历史任务杂乱排列，最近结束的任务没有稳定显示在最上方；用户无法快速找到刚生成的“审核服务稳健性、无人值守闭环与 Token 效率”报告。
- **影响：** 项目和任务积累后，最新交付被旧记录淹没；用户会误判产物缺失或任务未结束。
- **已验证证据：** Task `146cffbf-fe5f-4afe-a7d2-76606cc627cd` 已于 02:31:07 进入 `candidate_ready`，产物 `docs/audits/2026-07-21/unattended-token-review.md` 存在，修改时间 02:30:04、11,894 字节；该 Task 的 `completed_at` 为空，证明候选态不能依赖终态字段混排。当前 UI 的实际排序实现尚未在本条中做代码复验。
- **可能原因：** UI 可能沿用 API/创建/ordinal 顺序，或用缺失的 `completed_at` 对 candidate_ready 与真终态混排；需以当前代码复验后确认。
- **处理方式/结果：** 本次只记录并定位报告，不修改 UI、API 或当前 Goal。
- **关联风险点：** `completed_at=null`、cancelled/terminal_failed 时间来源、candidate_ready 被当终态、分页后局部排序、项目切换缓存、自动刷新旧响应覆盖、同秒完成顺序不稳定。
- **验收：** completed/terminal_failed/cancelled 按 canonical terminal event 时间降序；candidate_ready 独立分组并按候选事件时间降序；相同时间稳定排序；分页、筛选、项目切换和自动刷新后顺序一致；最新报告一屏内可见并可直接打开。

### I-015 独立验证 Task 在待执行与执行中反复横跳

- **日期/来源/状态：** 2026-07-21，8742 Task `0c289257-958f-402f-bce2-0c523d41c29a` 结构化事件与 Worker stream；`VERIFIED_OPEN`
- **故障现象：** “独立验证审核产物与证据完整性”在“待执行”和“执行中”泳道反复切换，用户无法判断它是在正常排队、恢复、重试还是再次从头执行。
- **影响：** 运行事实被错误 UI 语义掩盖；用户可能重复触发任务或中断有效执行。该 Task 最终累计消费 1,321,685 Token，耗尽 3/3 attempts，仍未形成通过验收的独立验证结果。
- **已验证证据：** attempt 1 于 02:38:13 开始，经历三次 `transient_provider_transport` resume 后于 02:47:09 产生 `task.retry_scheduled(passed=false)`；attempt 2 于 02:48:15 开始并于 02:51:09再次 `passed=false`；attempt 3 于 02:52:16 开始，02:53:06 因 transport defer，02:54:13 resume。Cursor stream 明确出现 `Aborted`、shell command 没有退出状态和“环境再次异常”；同时验证角色认为 A6 存在证据缺口，不能给出 PASS。
- **已验证原因：** 两种不同机制复用了同一个 `ready` 投影：Provider/工具传输故障触发同 attempt 的 defer/resume；EvidenceManifest 不通过触发新 attempt 的 retry。前端只按底层 Task status 分泳道，没有显示 `recovery_stage`、`last_error`、attempt、checkpoint 和 retry reason，因此表现为无原因横跳。
- **处理方式/结果：** 本次只读定位并记录；不修改活动 Task、不重试、不切换 Provider、不重启控制面。
- **最终结构化状态：** 2026-07-21 再查 Task 为 `terminal_failed`、revision 18、`same_failure_count=3`；最终错误为 `verification failed (CHANGES_REQUIRED): MODEL_TEXT_CHANGES_REQUIRED, STRUCTURED_VERDICT_NOT_PASS, REQUIRED_ACCEPTANCE_MISSING`。这证明此前泳道横跳没有收敛为成功，而是耗尽重试预算后失败。
- **关联风险点：** 第 3/3 attempt 失败后仍继续 resume、Provider 总体探测 healthy 但具体会话工具异常、同 Session 重复支付 Context、verification 角色创建子代理扩大执行、transport fault 与业务 CHANGES_REQUIRED 混合、证据失败缺少可见 acceptance ID。
- **验收：** UI 将普通 ready、retry_backoff、transport_recovering、verification_changes_required 和 provider/network suspended 分开显示；卡片展示 attempt/max、最近原因和下一次动作；同 attempt resume 不增加 attempt，新 attempt 必须由结构化失败证据触发；达到最大 attempt 后有界进入明确状态且不再回到普通 ready；Provider 工具故障不得伪装业务验证失败；状态转换组合测试和真实故障注入均无无原因横跳。

## 5. 当前不可忽略的开放风险

截至 2026-07-21 当前工作区：

- 分支为 `codex/butler-fail-closed`，基线 HEAD 为 `6708fa6a2e97d7a206a08e0fc70395a6077d5cbc`，存在大量未提交修改和未跟踪 `build/`。
- 8742 `/health` 返回 healthy，数据库 schema head 为 `0034_butler_planner_fail_closed.sql`、migration count 34；这不证明自然语言无人值守链路已闭环。
- 管家规划仍可能占用一次最长约 180 秒的同步 HTTP 请求。UI 已能显示持久 planning 状态，但请求生命周期、取消和 Provider 物理执行仍需要异步化/受控恢复验证。
- 当前候选代码、Git 提交、远端 main 和部署 image revision 并非同一状态；任何“已发布/已完成”结论必须逐项核对。
- 尚无证据证明一个全新自然语言目标在现行版本中无需人工修状态即可完成角色拆分、执行、产物生成和独立验证。

## 6. 宣布“无人值守可用”的最低门槛

只有同时满足以下条件，才能把当前系统称为“质量优先的无人值守自动化”：

1. 从全新项目、全新会话和自然语言指令开始，不依赖外部 Codex Goal。
2. 管家智能理解或一次一问澄清，主人只确认最终目标/边界/验收方案。
3. 自动选择角色、规则、Provider、依赖和产物路径，生成唯一幂等计划。
4. Worker 串并行关系正确，重试从结构化断点继续，没有重复 Planner、重复 Task 或重复物理进程。
5. required artifact 确实存在且哈希匹配，EvidenceManifest 绑定真实命令和验收项。
6. 独立验证给出结构化 PASS；任何 CHANGES_REQUIRED、网络/Provider suspension 或证据矛盾都不得伪装 completed。
7. 经历一次受控 Provider 中断和一次控制面重启后仍能有界续接。
8. 项目管家、任务页、Token 看板和告警显示同一个 canonical 状态。
9. Git 本地、提交、远端、部署镜像和数据库迁移均有单独证据。

在这条链路被实际跑通并保存证据前，测试通过只能证明候选机制，不得宣称整个系统已经可无人值守。
