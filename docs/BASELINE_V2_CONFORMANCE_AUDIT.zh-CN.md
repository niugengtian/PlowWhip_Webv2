# PlowWhip Web V2 基线独立符合性审计

## 1. 结论

**目标仓库不符合 V2 基线，当前不能通过 V2 基线验收。**

本结论来自真实入口、函数调用链、SQLite schema 和写入者、文件落盘、Provider/Checker/Host Bridge 数据流以及本地无副作用验证，不以 README、注释、测试名称、测试桩或模型自述作为“已实现”的证明。

目标仓库已经真实实现了 SQLite WAL、四个公开状态、TaskSession/SessionGeneration/HostJob、应用内 Cronner、项目租约、部分 Plan A/B、确定性任务闭环、独立 Checker TaskSession、Hot/Warm/Cold 框架、Token 记录以及只读 Monitor；但核心合同仍有系统性冲突：

1. 正式自然语言指令并非全部先由接入模型的 Planner 做语义分级；正则、关键词、kind 和步骤数直接成为最终分级权威。
2. `execution`、`verification`、`provider`、`cronner` 和初始化迁移代码都能直接更新 Task 生命周期字段，`advance_project` 不是唯一写入者。
3. Provider、Planner 和 Checker 的正式结果链依赖最后 100 行/65 KiB/32 KiB 截断内容；报告、计划和下游依赖没有完整交付合同。
4. Planner 模型产出和 minimal-token 模型探针没有统一经过独立 Checker；Checker 还读取执行 Worker 的自由输出派生报告。
5. Host Bridge 到时先 SIGTERM、固定等待 5 秒、再 SIGKILL，早于控制面完整 reconcile、Artifact/Evidence/handoff 保存，并可能继续 Provider fallback。
6. Session 冷归档会被截断覆盖，非故障 token 阈值也会创建新 generation，累计 token 快照又被调用方统一标成 `single`。
7. Secret 可随原始 message 进入 SQLite，Task 完成/取消没有显式使临时授权和 Secret 引用失效；部分硬约束只存在于 prompt。

编号条款共 **168** 项。结论统计：**已实现 102、部分实现 27、仅文档 0、未实现 3、与基线冲突 36**；优先级统计见第 5/7 节。

## 2. 审计范围与不可变边界

- 唯一基线：`/Users/niugengtian/work/plow-whip-web-v2/docs/MINIMAL_REDESIGN_BASELINE_V2.zh-CN.md`
- 基线：934 行、37,868 bytes、SHA-256 `4ae79ba906997640304a202ba0d453e2437e77e36468c29eb4a185ad970f6098`
- 目标仓库：`/Users/niugengtian/work/plow-whip-web_blue-1`
- 审计提交：`main@942f246d7b82dcfdcb74452c654646185a27067f`
- 审计 tree：`4b403873193070c374703129a75b28bbd0e53c26`
- 审计开始时 Git 状态：`## main`，干净
- 唯一新增文件：本报告
- 未修改：现有代码、测试、配置、数据库、Docker、任务、蓝绿环境和 Git 历史
- 未访问网络，未调用外部或付费 Provider，未部署、切流或提交 Git
- ponytail 明确关闭；未启用、读取或调用任何 ponytail 模式、skill 或插件

## 3. 方法与验证

### 3.1 方法

1. 先完整读取唯一基线，机械提取并去重 168 个编号条款。
2. 从 `python -m plowwhip serve`、HTTP GET/POST、Butler、`submit_message`、Cronner `tick`、`advance_project` 追踪真实主线。
3. 检查 `SCHEMA`、所有 `INSERT/UPDATE tasks`、TaskSession/Generation/HostJob、Artifact/Evidence、文件目录和只读连接。
4. 追踪 Planner/Worker/Checker 到 Host Bridge 的真实请求、结果读取、截断、验收、恢复与 Token 写入。
5. 阅读测试源码只用于寻找待核对路径；测试名称、mock 和断言不替代生产代码证据。
6. 只运行无网络、无持久文件写入的静态或内存内验证。

### 3.2 真实主线

```text
POST /api/messages
→ Butler 路由 / submit_message 写 messages
→ Cronner.tick 获取 project lease/fence
→ advance_project
→ normalize_instruction + classify_instruction
   ├─ simple/medium：直接建执行 TaskSession，跳过模型 Planner
   └─ large：Planner HostJob → Plan A/B JSON → 结构校验 → Task DAG
→ execution / verification / provider 内部直接更新 Task
→ checkpoint_project 写 Warm/Cold
→ Monitor 用只读连接读取 SQLite、文件和有界输出
```

### 3.3 精确代码证据索引

| 编号 | 精确位置 | 已核对事实 |
|---|---|---|
| E01 | `plowwhip/intake.py:20-49,790-917`; `plowwhip/planner.py:78-112`; `plowwhip/lifecycle.py:306-366` | regex/kind/步骤数/关键词先决定 spec 与最终 size；只有 large 进入模型 Planner。 |
| E02 | `plowwhip/planner.py:115-169,215-331`; `plowwhip/lifecycle.py:1452-1935,1996-2109,2148-2335` | Planner HostJob、A/B、0.95 门槛、DAG 结构验证、版本化安装和特制三步分支。 |
| E03 | `plowwhip/store.py:16-227,447-592,668-703` | 15 张应用表、四态、fault、WAL/事务/只读连接；初始化也更新 Task。 |
| E04 | `plowwhip/lifecycle.py:67-303`; `plowwhip/execution.py:489-510,1577-1619,1827-1849`; `plowwhip/verification.py:258-325,976-1039`; `plowwhip/provider.py:451-579`; `plowwhip/cronner.py:112-162` | 生命周期字段存在多个生产写入者。 |
| E05 | `plowwhip/app.py:273-312`; `plowwhip/cronner.py:19-30,33-181,206-234`; `plowwhip/__main__.py:17-30,72-79` | 一个应用内循环、文件锁、项目 lease/fence、`next_action` 查询和 candidate 禁用 Cronner。 |
| E06 | `plowwhip/execution.py:74-215,275-397,1523-1850` | Worker/TaskSession/Generation、冻结 settings/source、generation 轮转和 Provider fallback。 |
| E07 | `plowwhip/execution.py:400-545,1289-1462,2054-2061`; `plowwhip/provider.py:265-280`; `plowwhip/host_bridge.py:436-478`; `plowwhip/verification.py:1043-1159` | Artifact/Evidence；Provider 正式结果来自最后 100 行且 report 再截 32 KiB；Checker 输入合同。 |
| E08 | `plowwhip/continuity.py:38-218,221-454`; `plowwhip/host_bridge.py:708-735` | Hot/Warm/Cold；下游最多两个依赖且 report 32 KiB；Bridge 终态覆写为最多 256 KiB 尾部。 |
| E09 | `plowwhip/provider.py:451-579`; `plowwhip/execution.py:1402-1414`; `plowwhip/lifecycle.py:1730-1741`; `plowwhip/verification.py:775-785,2494-2506`; `plowwhip/monitor.py:161-217,335-399` | 归一化函数支持 single/cumulative，但所有调用点传 `single`；Task/Checker 汇总。 |
| E10 | `plowwhip/lifecycle.py:2363-2413`; `plowwhip/host_bridge.py:494-541,659-705`; `plowwhip/execution.py:683-698,789-793` | 控制面 deadline 路径与 Bridge runtime 路径不同；Bridge 固定 5 秒后强杀。 |
| E11 | `plowwhip/monitor.py:16-104,220-333,403-543,715-745`; `plowwhip/store.py:676-682` | Monitor `mode=ro`+`query_only`；默认 20 行/字节上限。 |
| E12 | `plowwhip/store.py:257-306,629-666`; `plowwhip/continuity.py:281-313,419-454`; `plowwhip/verification.py:329-405` | library 文件真源、原子 current/archive；首次 PASS 会自动提升项目 WorkerTemplate。 |
| E13 | `plowwhip/app.py:37-213`; `plowwhip/ui.py:71-77,117-206,223-369` | GET 只读、POST 仅 messages/actions；UI 有 7 个顶层视图及 Task 卡片。 |
| E14 | `plowwhip/provider.py:15-145,212-290,345-448`; `plowwhip/host_bridge.py:56-185,188-478,737-743,848-976` | 静态 Provider 候选顺序；Bridge token、路径/可执行白名单、PID/输出/Session。 |
| E15 | `plowwhip/intake.py:503-787`; `plowwhip/lifecycle.py:578-783,1169-1308`; `plowwhip/execution.py:1928-1971`; `plowwhip/git_publish_worker.py:89-289` | action、Git 授权绑定、恢复式删除提示、Git 远端证据和 cancel/rerun。 |
| E16 | `plowwhip/butler.py:19-51,54-230,233-271` | SQLite 精确搜索、项目路由、项目正文和 global 引用文件分离。 |
| E17 | `plowwhip/store.py:42-227`; 全仓库对象/状态静态搜索 | 无 Attempt/Episode/Run/Candidate/RoleInstance/ProviderInstance/SessionBinding/Verification Task。 |
| E18 | `Dockerfile:1-16`; `plowwhip/app.py:273-312`; `plowwhip/host_bridge.py:111-128` | 单镜像含 Web/Cronner；Host Bridge 是宿主进程边界；默认 8742/8765。 |
| E19 | `plowwhip/lifecycle.py:578-632`; `plowwhip/verification.py:407-517`; `plowwhip/execution.py:2044-2059` | Planner/Checker 的底层 spec 仍是 `provider_task`，会进入停止分支；`provider_probe` 与 `git_publish` 的活动 HostJob 不在该分支内，Task 可先标 cancelled。 |
| E20 | `plowwhip/intake.py:79-127`; `plowwhip/execution.py:34-38,1308-1315`; `plowwhip/host_bridge.py:994-1031`; `plowwhip/provider.py:345-354` | 有日志/env redaction，但 message 原文没有 Secret 拒收/脱敏，仍写 SQLite。 |
| E21 | `plowwhip/verification.py:329-405,976-989` | 首次 PASS 自动写项目模板；只有 revision>1 才要求“以后都这样”。 |
| E22 | `plowwhip/monitor.py:510-542,715-734`; `plowwhip/ui.py:173-175,257-302` | UI 显示绝对路径/卡片，但无按 Task ID 打开完整 Artifact/Session 的端点；Task model 字段不完整。 |

### 3.4 实际运行的验证

| 编号 | 命令/方式 | 结果 |
|---|---|---|
| V01 | `wc`、SHA-256、编号提取去重 | 基线 934 行、37,868 bytes、168 个唯一条款；hash 与第 2 节一致。 |
| V02 | `PYTHONDONTWRITEBYTECODE=1` 下用 Python `ast.parse` 解析 `plowwhip/*.py` 与 `tests/*.py` | 21/21 文件语法树解析成功；不等于语义符合。 |
| V03 | `sqlite3.connect(":memory:").executescript(SCHEMA)` 后查询 `sqlite_master` | 15 张应用表；Task 四态与 `one_active_task_per_project` 条件索引存在。 |
| V04 | 直接调用 `classify_instruction(content, kind)` 三组样例 | `write_text→simple`；“分析当前仓库并输出验收报告”→`medium`；含“前后端”的文本→`large`。证实最终分级由 kind/词表完成。 |
| V05 | `rg -n 'UPDATE tasks\|INSERT INTO tasks' plowwhip/*.py` | 命中 lifecycle、execution、verification、provider、cronner、store 六个模块。 |
| V06 | 13 个与基线关键路径直接相关的定向 `unittest`（vertical slice 10、review fixes 2、Host Bridge 1） | `Ran 13 tests in 2.333s`，`OK`；覆盖确定性闭环、DAG、large Planner、连续性、只读 Monitor、deadline、只读结果、Provider fallback、模糊派发、Token、写中断和 Cursor 累计用量解析。测试通过只证明这些测试路径，不能覆盖本报告指出的生产合同差距。 |
| V07 | 审计前后 `git status --short --branch` | 开始为干净 `## main`；报告写入后只允许本报告为 `??`，最终状态见第 8 节。 |

未运行全量测试、Docker、真实 Host Bridge、live API 或外部 Provider。定向测试使用测试自身的临时目录/内存状态；未创建仓库内测试文件或持久数据库。以下矩阵的“验证证据”明确区分生产调用链、内存验证、定向测试与未做 live 验证。

### 3.5 真正实现、文档声明与测试桩的判定边界

- **已实现**：必须有生产入口、真实调用链、持久化/文件结果及不可绕过的约束；README、注释、prompt 或模型自述不能单独升级结论。
- **部分实现**：存在真实生产路径，但覆盖范围、输入完整性、状态所有权、平台证明或 fail-closed 约束不足。
- **仅文档**：本次为 0；如果只有声明而没有生产入口，本报告不会记为已实现。
- **未实现**：P-12、R-14、D-24 未找到生产实现入口。
- **测试桩**：mock Provider/Bridge、测试名和断言只用于定位与验证特定路径；V06 全部通过也不反证生产合同中的结构性差距。
- **模型自述**：Planner JSON、Provider report、Checker 文本均按真实下游读取、校验和落盘方式判断，不把“我已完成”当成 Evidence。

## 4. 逐条符合性矩阵

结论枚举严格为：`已实现 / 部分实现 / 仅文档 / 未实现 / 与基线冲突`。E 编号指向精确生产代码；V 编号指向本次实际运行。`静态`表示逐函数核对但未调用真实 Provider/运行环境。

### 4.1 Planner（P-01～P-15）

| 条款 | 结论 | 精确代码证据 | 验证证据 | 差距 | 最小修正方向 |
|---|---|---|---|---|---|
| P-01 | 与基线冲突 | E01 | V04 | simple/medium 不调用模型 Planner。 | 所有正式 message 先产出校验后的 Planner 语义结果，再物化 Task。 |
| P-02 | 与基线冲突 | E01 `planner.py:78-105` | V04 | kind/关键词/步骤数是最终权威。 | 只把启发式作为 Planner 输入事实。 |
| P-03 | 已实现 | E01 | 静态 | 只有 simple/medium/large。 | 保持单一枚举。 |
| P-04 | 部分实现 | E01；E15 | V04 | write/probe 符合；git publish 被 kind 固定为 simple，实际含授权/远端验证。 | 由语义 Planner 证明“一个确定性动作”。 |
| P-05 | 与基线冲突 | `planner.py:101-105`; `lifecycle.py:355-364` | V04 medium 样例 | 未命中 large 的任意正式指令都交一个 fullstack Worker，无责任边界证明。 | Planner 输出并校验单一责任边界，否则升级。 |
| P-06 | 与基线冲突 | `planner.py:20-41,87-105` | V04 | 升级仅覆盖有限词表和声明步数。 | 模型结构化识别模块、角色、产物、依赖、Sprint、风险。 |
| P-07 | 已实现 | E02 `planner.py:120-143,221-223` | 静态 | 被判 large 后强制至少两个方案。 | 保持，并先修复分级入口。 |
| P-08 | 已实现 | E02 `planner.py:223-237` | 静态 | A/B 比较字段齐全。 | 增加真实性复核。 |
| P-09 | 与基线冲突 | E02 `lifecycle.py:1874-1935` | 静态 | 0.95 是 Planner 自报；业务取舍/授权仅用 regex flag 近似。 | 独立验证可客观选择事实；真实取舍交 Butler。 |
| P-10 | 部分实现 | E02 `planner.py:240-331` | 静态 DAG/环校验 | 只验证结构，不验证每 Task 的语义原子性。 | 校验单角色、单责任、完整输入/结果/验收。 |
| P-11 | 部分实现 | E02 `planner.py:121-139,243-310` | 静态 | 有角色、依赖、acceptance、Checker；缺强制结果类型/覆盖/文件路径格式和 large runtime。 | 扩充 TaskSpec schema 并 fail closed。 |
| P-12 | 未实现 | E01/E02 | 静态未找到升级入口 | 只有一次启发式初判，无证据化 simple→medium→large 修订。 | 让 Planner/Checker 提交升级事实，由 lifecycle 版本化 Plan。 |
| P-13 | 已实现 | E02 | 静态 | Planner 产出方案，状态安装在 lifecycle。 | 保持；其他模块写入另见 L/C。 |
| P-14 | 部分实现 | E07；`verification.py:1153-1158` | 静态 | Checker 可结构化 NEEDS_DECISION；Worker/Provider 输出未统一强制禁止直接提问。 | 所有角色只允许提交 blocker/decision fact。 |
| P-15 | 部分实现 | `lifecycle.py:2429-2471` | 静态 | 仅按 project/fingerprint 去重，多个项目可同时留等待问题。 | 明确全局或项目级不变量并用唯一约束实现。 |

### 4.2 生命周期所有权（L-01～L-06）

| 条款 | 结论 | 精确代码证据 | 验证证据 | 差距 | 最小修正方向 |
|---|---|---|---|---|---|
| L-01 | 与基线冲突 | E04 | V05 | 状态推进不只经过 `advance_project`。 | 非 lifecycle 模块只返回 facts；集中 reducer 写入。 |
| L-02 | 已实现 | `lifecycle.py:67-303` | 静态 | 每个 DB transaction 对应 prepare/apply/question 中一个明确动作。 | 为 action 增加类型化断言。 |
| L-03 | 与基线冲突 | E04 | V05 | Worker/Checker/Provider/Cronner 不只提交事实。 | 删除模块内 lifecycle SQL。 |
| L-04 | 已实现 | E11 | 静态+V03 连接约束核对 | Monitor `mode=ro` 且 `query_only`。 | 保持。 |
| L-05 | 已实现 | `store.py:117-122`; `cronner.py:53-87`; `lifecycle.py:212-301` | V03 | 非 queued active Task 有 DB 唯一索引；Goal 接入串行。 | 增加 active Goal 数据库断言。 |
| L-06 | 已实现 | `store.py:68-77`; `monitor.py:593-632` | V03+静态 | Goal 无 status，展示从 Task 聚合。 | 保持。 |

### 4.3 核心对象（O-01～O-17）

| 条款 | 结论 | 精确代码证据 | 验证证据 | 差距 | 最小修正方向 |
|---|---|---|---|---|---|
| O-01 | 已实现 | E03 projects/settings/library | V03 | Project 保存工作区、配置、规则索引、Goal 历史。 | 保持。 |
| O-02 | 已实现 | `store.py:68-77`; `lifecycle.py:372-397` | V03+静态 | Goal 字段含目标、边界、验收、revision；语义质量问题归 P。 | 保持对象，改 Planner 冻结来源。 |
| O-03 | 已实现 | `store.py:79-87`; E02 | V03 | Plan 有 revision/selected，无独立状态机。 | 保持。 |
| O-04 | 已实现 | `store.py:579-591`; `lifecycle.py:2266-2291` | V03 | Sprint 只是 Task 数字分组。 | 保持。 |
| O-05 | 部分实现 | `store.py:89-129`; E02 | 静态 | 可独立重试/验收，但语义原子性和完整结果未强制。 | 补 TaskSpec 结果合同与原子性 validator。 |
| O-06 | 已实现 | `store.py:204-215,257-305` | V03 | WorkerTemplate 以文件+library 索引表达。 | 保持；修模板污染路径。 |
| O-07 | 已实现 | `store.py:131-138`; `execution.py:117-127` | V03 | Worker 唯一键 project+role，不存物理 Session。 | 保持。 |
| O-08 | 已实现 | `store.py:140-149`; E06 | V03 | Task+Role 稳定槽；执行/Checker 分开。 | 保持。 |
| O-09 | 已实现 | `store.py:151-162`; E06 | V03 | 物理 Session 由 generation 表达。 | 修复非故障轮转，见 M-03。 |
| O-10 | 已实现 | `store.py:16-39`; E14 | V03 | HostJob 最小事实齐全，不拥有 Session。 | 保持。 |
| O-11 | 已实现 | `execution.py:128-156,275-292`; `host_bridge.py:288-319` | 静态 | session_id 只从当前 TaskSession generation 取。 | 增加稳定三元组审计字段。 |
| O-12 | 已实现 | `execution.py:74-156`; `lifecycle.py:424-478,2266-2303` | 静态 | 新 Task 创建独立 TaskSession/generation。 | 保持。 |
| O-13 | 已实现 | `execution.py:1753-1850` | 静态 | fallback 不重建 Task/TaskSpec/TaskSession，不删 Evidence。 | fallback 前强制最新有效 handoff。 |
| O-14 | 已实现 | E03/E17 | V03+静态搜索 | 无 Attempt/ExecutionEpisode/Run/Candidate 业务对象。 | 保持。 |
| O-15 | 已实现 | E03/E17 | V03+静态搜索 | 无 RoleInstance/ProviderInstance/SessionBinding。 | 保持。 |
| O-16 | 已实现 | `verification.py:196-257,460-483`; E17 | 静态 | 验证为同 Task 的 check HostJob，无 Verification Task/candidate_ready。 | 保持。 |
| O-17 | 已实现 | `store.py:16-39`; `monitor.py:454-462` | V03 | HostJob 序列/purpose/generation/spec_revision 足以留史。 | 保持。 |

### 4.4 状态（S-01～S-06）

| 条款 | 结论 | 精确代码证据 | 验证证据 | 差距 | 最小修正方向 |
|---|---|---|---|---|---|
| S-01 | 已实现 | `store.py:96-115`; `lifecycle.py:622-632` | V03 | cancelled 仅为 outcome，公开状态仍四态。 | 保持。 |
| S-02 | 已实现 | `store.py:96-115`; `lifecycle.py:179-209` | V03+静态 | plan/execute/check/repair/retry/provider_recovery/stopping 均为 phase。 | 可给 phase 加受控枚举。 |
| S-03 | 已实现 | `store.py:101-105` | V03 | fault_code 是基线七类。 | 保持。 |
| S-04 | 已实现 | E03/E17 | V03+静态搜索 | 无被禁止用户主状态。 | 保持。 |
| S-05 | 已实现 | `lifecycle.py:578-632`; 无 Artifact 删除路径 | 静态 | 取消保留 Task/workspace/Artifact/Evidence/log/handoff。 | 另修活跃 HostJob 停止，见 B-21。 |
| S-06 | 已实现 | `lifecycle.py:633-783` | 静态 | 同 task_id rerun，新 generation；spec 变化才 revision+1。 | 保持。 |

### 4.5 自动机制与恢复（A-01～A-25）

| 条款 | 结论 | 精确代码证据 | 验证证据 | 差距 | 最小修正方向 |
|---|---|---|---|---|---|
| A-01 | 已实现 | E05/E18 | 静态 | 镜像内一个 Cronner loop。 | 保持单例启动断言。 |
| A-02 | 已实现 | E05；无 crontab/launchd/systemd 运行依赖 | 静态搜索 | Cronner 完全应用内。 | 保持。 |
| A-03 | 部分实现 | E02 `planner.py:243-310` | 静态 | large 方案可写依赖/schedule/settings，但 simple/medium 跳过 Planner，且 max runtime 非必填。 | 统一 Planner 调度 schema 并强制 large 字段。 |
| A-04 | 已实现 | E02；无定时器生成路径 | 静态搜索 | Planner 只返回 JSON 事实。 | 保持。 |
| A-05 | 已实现 | `cronner.py:33-87`; `lifecycle.py:1162-1167` | 静态 | due 选择优先 `next_action_at/kind`。 | 保持。 |
| A-06 | 已实现 | `cronner.py:33-109,175-181` | 静态 | 每 tick 只读取当前到期事实，不补逐秒历史。 | 保持。 |
| A-07 | 部分实现 | E05 `cronner.py:19-30,112-162`; projects lease 字段 | V03+静态 | 单 data root 有锁/租约/fence；没有跨错误挂载/不同 data root 的生产唯一资格证明。 | 部署层固定唯一 production identity，并在 DB 记录实例资格。 |
| A-08 | 已实现 | `verification.py:877-956`; `execution.py:1289-1462` | 静态 | read-only/probe 可用结构化 Evidence 完成，无需 diff。 | 保持，但修完整输出来源。 |
| A-09 | 已实现 | E03/E17；无 zero_progress 状态 | V03+静态搜索 | 未因无 diff/日志制造新状态。 | 保持。 |
| A-10 | 已实现 | `execution.py:1523-1538,1667-1671`; E15 | 静态 | unsafe external interruption fail closed，不盲重放。 | 把 timeout 写操作也纳入 unsafe 判断。 |
| A-11 | 已实现 | `execution.py:1753-1850` | 静态 | 普通 terminal provider failure 在预算内递补。 | 保持并先保存 handoff。 |
| A-12 | 已实现 | `execution.py:1481-1538,1841-1850`; `provider.py:543-579` | 静态 | credential/scope/cost/auth/候选耗尽进入 needs_decision。 | 保持结构化原因。 |
| A-13 | 已实现 | `execution.py:1523-1588`; HostJob terminal 判定 | 静态 | running HostJob 不切 Provider。 | 保持。 |
| A-14 | 部分实现 | `execution.py:1753-1850`; `continuity.py:332-454` | 静态 | 旧 generation 会归档，但 fallback 在本轮 checkpoint 前创建，新 generation 可能只拿旧 handoff_ref。 | fallback 前同步产生并校验最新 Warm handoff，再归档/替换。 |
| A-15 | 已实现 | `verification.py:47-195,258-325` | 静态 | 纯确定性路径可只做确定性验证。 | 保持。 |
| A-16 | 与基线冲突 | E02；`verification.py:2453-2524` | 静态 | Worker 模型任务有 Checker；Planner 模型方案和 minimal-token 模型探针没有独立 Checker。 | 所有模型产出先写正式结果，再由独立 Checker 验收。 |
| A-17 | 与基线冲突 | E07 `verification.py:1118-1159` | 静态 | 虽用独立 TaskSession，但 Checker prompt 读取 Provider 自由输出派生的 report。 | Checker 只读正式 Artifact/Evidence/diff/handoff，不读聊天派生文本。 |
| A-18 | 与基线冲突 | E07/E08 | 静态 | Checker 未取得完整 diff/所有依赖/完整超长 Artifact；输入会被截断。 | 建 manifest，按 path+hash+revision 覆盖读取全部正式输入。 |
| A-19 | 已实现 | `verification.py:1162-1235` | 静态 | CHANGES_REQUIRED 含 acceptance_id、实际/预期、scope、recheck。 | 保持 schema 校验。 |
| A-20 | 已实现 | `verification.py:258-325,976-1039`; E06 | 静态 | repair 复用原 Task/执行 TaskSession，无 Candidate/验证 Task。 | 保持。 |
| A-21 | 已实现 | E11 | 静态 | Monitor 只读 SQLite/workspace/Artifact/Evidence/handoff/有界日志。 | 保持。 |
| A-22 | 已实现 | `monitor.py:737-745`; `store.py:230-254` | 静态 | 默认 20 行且受 byte cap。 | 保持可配置来源显示。 |
| A-23 | 与基线冲突 | E07/E08 | 静态 | 正式 Provider/Planner/Checker 结果从最后 100 行/截断 report 获取，尾部成为下游/完成输入。 | 完整结果单独落盘并哈希；tail 只留 UI/诊断。 |
| A-24 | 已实现 | `task_events` 写入点；无 Monitor sample/SSE history 表 | V03+静态 | 只保存真实里程碑，不补录采样。 | 保持。 |
| A-25 | 已实现 | E05/E11；`host_bridge.py:543-706` | 静态 | 恢复读取当前 DB/HostJob 事实和有界日志，不回放全史。 | 保持；完整 Cold 另修。 |

### 4.6 完整结果（R-01～R-17）

| 条款 | 结论 | 精确代码证据 | 验证证据 | 差距 | 最小修正方向 |
|---|---|---|---|---|---|
| R-01 | 部分实现 | `store.py:89-115`; E07 | V03+静态 | Task 有 acceptance/outcome，确定性结果持久化；通用模型 TaskSpec 没有强制完整结果类型。 | 给每个 TaskSpec 定义 result contract。 |
| R-02 | 与基线冲突 | E07 `execution.py:1289-1462` | 静态 | 报告/计划/审查可仅生成 data-root、最多 32 KiB 的 `provider-report.md`，不是独立完整 workspace Artifact。 | 交付型 Task 强制写 TaskSpec 路径，登记 hash/revision。 |
| R-03 | 与基线冲突 | E07/E08 | 静态 | stdout 尾部、聊天派生 report/handoff 代替正式完整交付。 | 分离 observation、raw log、正式 result。 |
| R-04 | 与基线冲突 | `intake.py:821-917`; E02 | V04+静态 | 通用 TaskSpec 缺强制结果类型/覆盖；文件路径/格式非统一 schema。 | 新增受控 result 字段并在 install 前校验。 |
| R-05 | 部分实现 | `store.py:166-178`; `execution.py:400-511,1366-1439` | V03+静态 | 有 path/hash/revision/task/kind；缺独立 scope 字段，部分来源只隐含在 metadata。 | Artifact manifest 显式记录 scope/source_task。 |
| R-06 | 与基线冲突 | E08 `continuity.py:136-218` | 静态 | 下游只取最多两个依赖，优先截断 report/evidence，不强制 path+hash+revision。 | 枚举全部 DAG 前驱，以 manifest 引用完整 Artifact/Evidence。 |
| R-07 | 与基线冲突 | E08 | 静态 | 没有覆盖完整超长文件的分段协议，只读摘要/尾部。 | 按 chunk manifest 读全文件并记录覆盖范围/hash。 |
| R-08 | 部分实现 | `verification.py:1118-1235` | 静态 | acceptance 映射严格；完整性/覆盖范围因输入截断不能成立。 | Checker 先验 manifest 完整性，再验每个 acceptance。 |
| R-09 | 已实现 | `verification.py:196-257,460-483` | 静态 | 内部探针为同 Task 的 check/command HostJob。 | 保持。 |
| R-10 | 已实现 | `intake.py:790-820`; `verification.py:2399-2524` | 静态 | 主人明确 probe 可成独立 Task。 | 保持。 |
| R-11 | 已实现 | `verification.py:2453-2524` | 静态 | 结果含 target/time/mode/returncode/observation/verdict。 | 把 execution method 字段命名得更明确。 |
| R-12 | 与基线冲突 | `host_bridge.py:436-478,708-735`; E07 | 静态 | 原始输出终态被覆写为尾部且下游用该尾部；不能提供完整有界引用/hash。 | 原始输出 append-only 分段，另生成 tail view。 |
| R-13 | 已实现 | `verification.py:258-325,976-1039`; `execution.py:2335-2434` | 静态 | 模型声明、exit 0、文件/token 均不能单独将模型 Task 标 done；需 Checker/Evidence。 | 保持并覆盖 Planner/probe。 |
| R-14 | 未实现 | 全仓库搜索 `audit-report`/审查前置 gate | 静态未找到 | 没有“完整 audit-report.md PASS 后才能规划修复”的生命周期 gate。 | 把审查 Artifact+Checker PASS 建为 repair Plan 的硬依赖。 |
| R-15 | 与基线冲突 | E08 | 静态 | 后续 Task 可依据截断 report/handoff，不能保证完整审查报告输入。 | 修复计划只接受报告 Artifact manifest。 |
| R-16 | 部分实现 | `execution.py:664-1442`; `verification.py:877-956` | 静态 | 有 before/after fingerprint、检查 Evidence；未持久化职责闭合的完整 diff/revision manifest。 | 保存 diff/revision、测试、Evidence 的完整结果 manifest。 |
| R-17 | 已实现 | E03/E17 | V03 | 复用 TaskSpec/Artifact/Evidence，无第二结果状态机。 | 保持。 |

### 4.7 会话、三层记忆与 Token（M-01～M-17）

| 条款 | 结论 | 精确代码证据 | 验证证据 | 差距 | 最小修正方向 |
|---|---|---|---|---|---|
| M-01 | 已实现 | E06/E08；`host_bridge.py:870-910` | 静态 | Provider native compact 与本地 handoff/segment 并行。 | 保持。 |
| M-02 | 已实现 | E14；无修改 Provider 原生 session 文件路径 | 静态 | 只保存引用/PlowWhip 副本，不改原生文件。 | 保持。 |
| M-03 | 与基线冲突 | `execution.py:1540-1542,1674-1750` | 静态 | 非 Codex 仅因 context token 阈值也创建新 generation，并非不可恢复或 Provider 递补。 | 可恢复时原 session 继续/compact；只有明确不可恢复才 replacement。 |
| M-04 | 已实现 | `continuity.py:38-133` | 静态 | Hot 临时编译，无第二 current.md。 | 保持。 |
| M-05 | 已实现 | `continuity.py:332-454` | 静态 | 每 Task+Role 有结构化 current.json 恢复入口。 | 保持。 |
| M-06 | 与基线冲突 | E08 | 静态 | 有 Cold 目录/manifest，但 Bridge 先截断并覆写原输出，故不是完整分段会话/日志历史。 | 原始流 append-only segment；manifest 引用全部历史/hash。 |
| M-07 | 已实现 | `store.py:230-254`; E06/E08 | V03+静态 | handoff/checkpoint/context/observation/rotation 均有可配置 setting。 | 保持，避免 Bridge 固定 5 秒。 |
| M-08 | 已实现 | `execution.py:192-215` | 静态 | effective settings 顺序 global→project→task_role，后者覆盖。 | UI 显示来源。 |
| M-09 | 部分实现 | `continuity.py:108-132,419-454` | 静态 | 超限会拒绝，不静默丢；提交前缺冲突预警和有效值/来源展示。 | submit 时计算预算并 warning/reject。 |
| M-10 | 已实现 | `provider.py:451-579` | 静态 | cached 被校验为 input 子集，normalized 不重复加 cached。 | 保持。 |
| M-11 | 部分实现 | E09 | 静态 | 函数能记录 single；调用者没有 adapter 级证据区分。 | Adapter 返回明确 usage_kind。 |
| M-12 | 与基线冲突 | E09 | 静态 | 归一化支持 cumulative，但 Planner/Worker/Checker/probe 调用全部硬传 `single`，恢复会重复计累计快照。 | 保存 raw kind/snapshot，并按同 physical session 相邻差值。 |
| M-13 | 已实现 | `provider.py:486-538`; session_generation 外键 | V03+静态 | 累计基线按 generation/session 隔离。 | 先修调用 kind。 |
| M-14 | 已实现 | `provider.py:582-597`; `execution.py:216-251` | 静态 | Task 预算汇总所有 model_calls，不随 generation 重置。 | 保持。 |
| M-15 | 已实现 | `verification.py:775-785`; `monitor.py:335-399` | 静态 | Checker 计入 Task 且按 checker TaskSession/role 显示。 | 保持。 |
| M-16 | 已实现 | E08；无 model_call 于 stat/hash/rotation/checkpoint | 静态 | 文件操作和确定性 handoff 零模型调用。 | 保持。 |
| M-17 | 已实现 | 全仓库 waste/cached 语义搜索；E09 | 静态 | 未把 cached context 一概标浪费，也无全量日志回放计费。 | 若未来加 waste 指标，必须保存内容证据。 |

### 4.8 超时（T-01～T-10）

| 条款 | 结论 | 精确代码证据 | 验证证据 | 差距 | 最小修正方向 |
|---|---|---|---|---|---|
| T-01 | 已实现 | `store.py:230-254`; E06 | V03+静态 | 600 仅为可覆盖 global fallback。 | 保持。 |
| T-02 | 与基线冲突 | E02 `planner.py:121-139,243-310` | 静态 | large Task 的 settings/max_runtime 可省略，normalize 不强制每 Task 设置。 | Planner schema 将 `max_runtime_seconds` 设为 required。 |
| T-03 | 已实现 | `execution.py:192-215`; default WorkerTemplate/settings | 静态 | simple/medium 使用模板/Project/Global 生效值。 | 保持。 |
| T-04 | 已实现 | `execution.py:192-215` | 静态 | Task+Role > Project > Global。 | 保持。 |
| T-05 | 已实现 | `execution.py:74-156,192-215`; `store.py:140-149` | V03+静态 | TaskSession 冻结 setting_snapshot 及 source。 | UI 显示全部来源。 |
| T-06 | 与基线冲突 | E10 `host_bridge.py:494-541,659-705` | 静态 | Bridge max runtime 到时先杀进程，未先控制面 reconcile/保存 Artifact/Evidence/handoff。 | Bridge 只报告 deadline fact；lifecycle 先 reconcile 和 checkpoint。 |
| T-07 | 与基线冲突 | E10 | 静态 | Bridge 固定等待 5 秒，忽略冻结的 `stop_grace_seconds`。 | grace 由 TaskSession setting 传入并由 lifecycle 驱动。 |
| T-08 | 已实现 | `host_bridge.py:494-541`; `store.py:96-115` | V03+静态 | timeout 记录在 HostJob failure 事实中，没有新增用户 `timed_out` 状态。 | 保持；终止次序问题单列 T-06/T-09。 |
| T-09 | 与基线冲突 | E10；`execution.py:1523-1850` | 静态 | timeout 后先终止，且 write timeout 不一定被 `_write_interruption_is_unsafe` 拦住，可自动 fallback。 | reconcile 真实结果后由 lifecycle 选择 verify/recover/new generation/fallback/decision。 |
| T-10 | 已实现 | timeout 判定只比较 runtime/deadline；无 no-output fail path | 静态搜索 | 无输出不单独触发超时/失败。 | 保持。 |

### 4.9 SQLite、文件、模板、Secret（D-01～D-30）

| 条款 | 结论 | 精确代码证据 | 验证证据 | 差距 | 最小修正方向 |
|---|---|---|---|---|---|
| D-01 | 已实现 | E03 `store.py:668-674` | V03 | SQLite WAL 是唯一状态库/队列。 | 保持。 |
| D-02 | 已实现 | 依赖/代码搜索；E03 | 静态 | 无 Redis/RabbitMQ/Celery/第二队列。 | 保持。 |
| D-03 | 已实现 | `store.py:54-66`; `butler.py:54-271` | V03+静态 | messages 保存 scope/message/action/决定及去重键。 | 保持。 |
| D-04 | 已实现 | `store.py:89-129`; `cronner.py:33-109` | V03 | tasks 即 due 执行队列。 | 保持。 |
| D-05 | 已实现 | `store.py:180-187`; task_events 写入点 | V03+静态 | 只记里程碑，无 Monitor sample。 | 保持。 |
| D-06 | 部分实现 | `store.py:166-178` | V03 | path/hash/revision/kind/bytes 齐；范围只隐含在 acceptance/metadata，无 scope 列。 | manifest 显式记录 scope 与 source。 |
| D-07 | 已实现 | `store.py:43-52,107-110`; E05 | V03 | lease 在 projects；逻辑闹钟在 tasks。 | 保持。 |
| D-08 | 已实现 | E03/E17 | V03 | 15 表可表达当前对象，没有重复运行表。 | 新表继续要求不可表达性论证。 |
| D-09 | 与基线冲突 | E04 | V05 | 多模块和 Cronner catch 绕过 `advance_project` 写 Task。 | 单一 lifecycle reducer。 |
| D-10 | 已实现 | `continuity.py:221-314`; `execution.py:400-511` | 静态 | 目录含 project/task/role/generation/revision。 | 统一所有 Artifact 路径层级。 |
| D-11 | 与基线冲突 | `host_bridge.py:708-735`; E08 | 静态 | Bridge 终态会用最多 256 KiB 尾部覆写 stream log；不是只增新 segment。 | 原始 segment 永不覆写，tail 单独生成。 |
| D-12 | 已实现 | `continuity.py:419-454`; atomic write helper | 静态 | current 原子替换，旧版入 archive。 | 保持。 |
| D-13 | 与基线冲突 | E07 `execution.py:1289-1462`; TaskSpec 无交付 path | 静态 | 主人要求报告/计划时可落 data root 副本而非指定 workspace 单一真源。 | TaskSpec 指定 workspace path；Artifact 只索引该文件。 |
| D-14 | 已实现 | E08/E14；data root 路径检查 | 静态 | 内部日志/handoff/session/evidence 在 data root。 | 保持。 |
| D-15 | 已实现 | `app.py:37-213`; E22 | 静态 | 浏览器只传 ID/action，无任意本地 path 读取 API。 | 增加安全的 Task-ID 文件打开端点。 |
| D-16 | 已实现 | 全仓库运行时文件搜索 | 静态 | 未生成 AGENT_STATE/CURRENT_STATUS/NEXT_ACTION/AGENT_COMMS 第二真源。 | 保持。 |
| D-17 | 已实现 | `store.py:257-306,629-666`; library 目录 | 静态 | 角色/规则/模板/脚本正文以文件为真源。 | 保持。 |
| D-18 | 已实现 | `store.py:204-215` | V03 | library_items 只存 kind/scope/revision/path/hash 等索引。 | 保持。 |
| D-19 | 已实现 | `execution.py:74-156,192-215` | 静态 | TaskSession 冻结角色、规则、Provider 顺序、阈值。 | 保持。 |
| D-20 | 部分实现 | E14/E15；Worker/Checker prompts | 静态 | 路径/可执行/bridge token 有代码硬约束；删除、不可逆、Checker 独立等部分仅靠 prompt/分支。 | 统一结构化授权与不可绕过的 runtime policy。 |
| D-21 | 与基线冲突 | E21 | 静态 | 每个项目的首次模型 Task PASS 都自动创建 WorkerTemplate；Task 中形成的临时做法可在无主人长期决定时进入模板。 | 删除自动 promotion；只消费显式、结构化的 owner 长期规则决定。 |
| D-22 | 与基线冲突 | E21 | 静态 | 首次 PASS 无需主人说“以后都这样”就更新项目 WorkerTemplate。 | 默认不 promotion；仅消费结构化 owner decision 更新 revision。 |
| D-23 | 已实现 | `store.py:257-306`; 默认 library 文件 | 静态 | 模板正文无物理 session/task/授权/secret/handoff 特例。 | 更新模板时继续 schema 扫描。 |
| D-24 | 未实现 | library/verification 全路径搜索 | 静态未找到 | 没有“当前 Task 验收后再登记新脚本”的通用流程。 | 增加受 lifecycle/Checker gate 的显式 script promotion action。 |
| D-25 | 部分实现 | `git_publish_worker.py:89-289`; CLI 入口 | 静态 | 内建 Git worker 符合函数模块+CLI/exit/stdout/stderr；没有通用脚本库合同。 | 抽出最小脚本登记规范，不建框架。 |
| D-26 | 已实现 | 依赖与 script 搜索 | 静态 | 优先标准库/系统命令，无 BaseScript/Factory/plugin framework。 | 保持。 |
| D-27 | 已实现 | `execution.py:192-215` | 静态 | Task+Role > Project > Global。 | 保持。 |
| D-28 | 与基线冲突 | `host_bridge.py:994-1031`; `__main__.py` | 静态 | env 除部署地址/端口/data root/Secret 引用外，还承载 `DEEPSEEK_MODEL`、`KIMI_MODEL` 等 Provider 业务选择。 | 模型与 Provider 业务选择迁入 Global/Project/Task+Role settings，env 仅保留部署级项。 |
| D-29 | 已实现 | `store.py:230-254`; Docker 配置检查 | 静态 | 业务阈值主要在 settings，不散落 Docker env。 | 保持。 |
| D-30 | 与基线冲突 | E20；`intake.py:79-127` | 静态 | 日志有 redaction，但用户 message 原文可含 Key/Token 并原样写 SQLite；没有 intake 脱敏/拒绝。 | 入库前检测 Secret，只存 opaque reference；覆盖 message/prompt/template/log。 |

### 4.10 部署、Provider、Butler、页面、权限（B-01～B-21）

| 条款 | 结论 | 精确代码证据 | 验证证据 | 差距 | 最小修正方向 |
|---|---|---|---|---|---|
| B-01 | 已实现 | `Dockerfile:1-16`; E05/E13/E14；模块 import/调用关系 | 静态 | Monitor、Planner、Checker、Provider Pool 均为同进程 Python 模块，没有网络微服务边界。 | 保持。 |
| B-02 | 部分实现 | `host_bridge.py:56-185,188-541,543-735,848-976` | 静态；未做 macOS/Linux live | Bridge 管理进程、PID、退出码、workspace、Session 和输出；代码使用 POSIX 进程语义，但本次未证明 macOS/Linux 两个平台均可运行。 | 在两个受控平台做相同的 start/read/stop/resume/segment 契约测试并保存 Evidence。 |
| B-03 | 已实现 | `plowwhip/__main__.py:17-30,72-79`; `app.py:273-312` | 静态 | 默认监听 `127.0.0.1:8742`，参数/部署配置可覆盖。 | 保持 loopback 默认值。 |
| B-04 | 部分实现 | `provider.py:15-145,451-579` | 静态 | 候选、能力、可用事实和顺序真实存在；同模块还承担 ModelCall 归一化及 Task 预算处置，越过“只管理”边界。 | Provider 返回可用性与用量 facts；预算与 Task 决定移入 lifecycle。 |
| B-05 | 与基线冲突 | `provider.py:451-579` | V05 | Provider 预算函数可直接把 Task 更新为 `needs_decision`，拥有了 Task 生命周期决定权。 | 只返回 `budget_exceeded` fact，由 lifecycle reducer 写 Task。 |
| B-06 | 已实现 | `provider.py:15-145`; `execution.py:1753-1850` | 静态 | 使用静态有序候选并串行递补；无动态竞价、评分或并行竞跑。 | 保持。 |
| B-07 | 已实现 | `monitor.py:345-399`; `ui.py:343-369`; Provider probe 调度搜索 | 静态 | 没有周期性付费探活；minimal-token probe 是主人显式确认后创建的 Task。 | 保持，并让模型探针也进入独立 Checker。 |
| B-08 | 已实现 | `butler.py:79-187`; `ui.py` 首页 message form | 静态 | 统一窗口默认由全局路由入口接待。 | 保持。 |
| B-09 | 已实现 | `butler.py:42-76,79-187`; `ui.py` `currentProject`/`loadButler` | 静态 | 路由后同一 UI 切 active project 并读取项目消息历史。 | 保持。 |
| B-10 | 已实现 | `butler.py:173-230`; `intake.py:79-127` | 静态 | 正文写项目 message；global conversation 文件只保存 message/project 引用，不复制正文。 | 保持并对正文做 Secret intake 过滤。 |
| B-11 | 已实现 | `butler.py:19-230` | 静态 | Butler 只搜索、路由或进入创建项目 action；不直接写 Task 生命周期字段。 | 保持。 |
| B-12 | 部分实现 | E16 | 静态 | 精确查询先走 SQLite/文件索引；未实现需要语义归纳时调用模型的受控分支，只能 regex/报错/人工明确。 | 增加只读、低预算、独立可验收的语义归纳路径。 |
| B-13 | 部分实现 | `monitor.py:510-542,715-745`; `ui.py:173-175,257-302` | 静态 | API 有 `recent_output`/20 行上限，但 Task UI 没有始终明确标为“观察信息、不可作为 Evidence”；生产链另有尾部充当输入的问题。 | UI/API 字段命名为 observation_tail，并显示非 Artifact/非 Evidence 警示。 |
| B-14 | 部分实现 | `ui.py:71-77,117-206,223-369` | 静态 | 没有 Worker/Provider/Attempt/Episode/Candidate 独立页；但实际保留 7 个顶层视图，不符合章节“页面只保留四类”的整体合同。 | 收敛成全局首页、项目详情、Task 详情、设置与资源库；Monitor/Token 嵌入这些页面。 |
| B-15 | 已实现 | `app.py:37-213` | 静态 | 写入口收敛为 `/api/messages` 与 `/api/actions`。 | 保持有限 action allowlist。 |
| B-16 | 已实现 | `app.py:37-213`; `intake.py` 各写入口 | 静态 | GET 使用只读 snapshot；POST 只提交 message/action 且强制 idempotency key。 | 对未来外部 Agent 保持同一约束。 |
| B-17 | 部分实现 | `intake.py:503-787`; `execution.py:34-38,275-397`; Worker prompts | 静态 | 只读与工作区内恢复式修改可自动调度；部分 workspace/恢复式约束依赖 prompt，缺统一 runtime policy。 | Host Bridge 根据结构化 capability/action/scope 强制执行三档权限。 |
| B-18 | 部分实现 | E15；Git publish 授权路径；通用 action allowlist | 静态 | Git 外部发布、force-with-lease 有显式授权；永久删除、迁移、切流、外部发送/付款、权限变更等没有统一结构化授权执行器。 | 建最小通用 authorization fact 和 action policy，未知不可逆动作 fail closed。 |
| B-19 | 部分实现 | Worker/Checker prompt 中 `.rm.<timestamp>` 指令；Host Bridge 文件操作路径 | 静态 | 恢复式删除约定只在 prompt，没有执行层拦截永久删除并改写为 `.rm.年月日时分秒`。 | 在 Bridge/受控文件工具硬执行 rename-to-rm。 |
| B-20 | 已实现 | `intake.py:503-787`; `lifecycle.py:1169-1308`; `git_publish_worker.py:89-289` | 静态 | 授权绑定 project、task、spec_revision、action_kind、target_scope、expiry，并在执行前复核。 | 保持一次性 nonce/fence。 |
| B-21 | 与基线冲突 | `lifecycle.py:578-632`; Task 完成路径；authorization JSON/Secret env 路径 | 静态 | done/cancel 没有显式撤销临时授权或 Secret 引用；`provider_probe`/`git_publish` 的活动 HostJob 还可能在 Task 先取消后继续。 | 终态事务撤销授权/Secret ref，并对所有 active HostJob 发 stop、等待终态后再完成取消。 |

### 4.11 模块边界（C-01～C-04）

| 条款 | 结论 | 精确代码证据 | 验证证据 | 差距 | 最小修正方向 |
|---|---|---|---|---|---|
| C-01 | 与基线冲突 | E01 | V04 | intake/planner 的 regex、kind、步骤数和词表取代模型 Planner，成为正式分级权威。 | intake 只规范化；Planner 模型返回结构化分级并由 lifecycle 校验。 |
| C-02 | 与基线冲突 | E04 | V05 | execution、verification、provider、cronner、store 都能写 Goal/Task 生命周期相关字段。 | 集中为一个 lifecycle reducer；其余模块只提交不可变 facts。 |
| C-03 | 已实现 | 全模块 import/调用图；E18 | 静态 | 应用模块之间直接函数调用；只有宿主边界使用 Host Bridge HTTP。 | 保持。 |
| C-04 | 部分实现 | E03/E17；E04；按 kind/phase 的生产分支 | V03+V05 | 没有被禁止的状态/对象/补丁表/网络推进器，但多个模块写生命周期，且 probe/git/planner 等特制分支绕开统一 reducer。 | 先统一 facts→reducer；不得用新状态或补丁表修复现有分支。 |

## 5. 合并后的 P0～P3 差距

同一根因只列一次；“数量”指本节的合并根因数，不是受影响条款数。

### P0：主线与完成可信度（6）

| 根因 | 受影响条款 | 证据与风险 | 最小修正方向 |
|---|---|---|---|
| 正式指令绕过模型 Planner | P-01/P-02/P-05/P-06/C-01 | E01/V04；simple/medium 由启发式决定，无法证明语义分级、责任边界与升级。 | 所有正式指令先生成结构化 PlannerResult；启发式只作事实输入。 |
| large 方案选择与原子合同不足 | P-09/P-10/P-11/P-12 | E02；A/B 和 DAG 真实存在，但 0.95 为自报，原子性、结果路径、覆盖和升级未强制。 | 定义 PlannerResult/TaskSpec JSON schema，独立验证客观选择条件，业务取舍只问主人。 |
| lifecycle 非唯一写入 | L-01/L-03/D-09/B-04/B-05/C-02/C-04 | E04/V05；六个模块命中 Task SQL，导致恢复、预算、检查和 Cronner 可各自决定状态。 | 一个 `advance_project` reducer 消费 facts，所有 Task/Goal 生命周期 SQL 只留在 lifecycle。 |
| 正式结果、Artifact 与下游输入不完整 | A-23/R-01～R-08/R-12/R-14～R-16/D-06/D-11/D-13 | E07/E08；100 行、65 KiB、32 KiB 和 256 KiB 尾部进入交付、Checker 或依赖链。 | 每 Task 强制 result contract；完整 Artifact/结构化 Evidence 以 path+hash+revision manifest 传递，tail 永不作输入/证据。 |
| 模型产出未统一独立验收 | A-16/A-17/A-18/R-08/R-13 | Planner/probe 不走 Checker；Worker Checker 又读取自由输出派生、截断 report。 | 所有模型产出先落正式结果；独立 Checker 只按完整 manifest、workspace、diff、Evidence、必要 handoff 验收。 |
| 取消/完成没有收敛外部影响 | B-21/S-05 | E19；probe/git HostJob 与授权/Secret ref 可晚于 Task 终态继续有效。 | 终态前停止并 reconcile 全部 HostJob，原子撤销 authorization/Secret ref，再提交 Task outcome。 |

### P1：恢复、计费与安全（6）

| 根因 | 受影响条款 | 证据与风险 | 最小修正方向 |
|---|---|---|---|
| timeout 次序与 grace 冲突 | T-02/T-06/T-07/T-09 | E10；Bridge 先 SIGTERM、固定 5 秒、再 SIGKILL，可能丢结果并继续 fallback。 | TaskSession 冻结 runtime/grace；Bridge 只报 deadline fact，lifecycle 先 reconcile/checkpoint 后再停止。 |
| Generation 与 handoff/Cold 不满足无损恢复 | A-14/M-03/M-06/M-09 | E06/E08；健康 Session 可因 token 阈值换 generation，旧输出被尾部覆写，fallback 未保证最新 handoff。 | 仅不可恢复/递补时换 generation；原始分段 append-only；替换前原子保存并验证最新 Warm。 |
| 累计 Token 被调用方误标 single | M-11/M-12/M-13 | E09；归一化原语存在，但实际 Planner/Worker/Checker/probe 调用统一传 `single`。 | Adapter 返回 usage_kind/raw_snapshot/physical_session_id；仅聚合同 Session 相邻差值。 |
| Secret 可进入 SQLite/Prompt | D-30/B-21 | E20；message 原文无拒收/脱敏，终态也不撤销引用。 | intake 识别 Secret，仅保存 opaque ref；终态撤销，日志/prompt/template 全链路验证。 |
| 安全策略部分只靠 prompt | D-20/B-17/B-18/B-19 | E15；删除与通用不可逆动作没有 runtime 强制。 | 用三档结构化 capability + scope + expiry 在 Host Bridge/worker 前硬校验。 |
| PASS 自动污染项目模板 | D-21/D-22 | E21；首次 PASS 无主人“以后都这样”即创建模板。 | 删除自动 promotion；显式 owner 长期决定才创建新 revision。 |

### P2：产品与配置收敛（4）

| 根因 | 受影响条款 | 证据与风险 | 最小修正方向 |
|---|---|---|---|
| 页面与完整文件入口未收敛 | B-13/B-14；章节 Task 详情合同 | E22；7 个顶层视图，只有路径文本，没有按 Task ID 安全打开完整 Session/Artifact 的端点。 | 收敛四类页面；新增后端 ID→受控路径→hash 校验的只读文件入口。 |
| Provider 业务选择仍在 env | D-28/D-29 | `host_bridge.py:994-1031`；模型名与部署项混放。 | 模型/Provider 业务项迁到 SQLite settings 并冻结来源。 |
| 新脚本入库验收链缺失 | D-24/D-25 | 未找到通用登记 gate；只有内建 Git CLI。 | 使用现有 Task/Checker/library_items 增加一个显式 promotion action，不建脚本框架。 |
| Butler 缺语义归纳分支 | B-12 | E16；精确查找存在，语义场景只能规则匹配或要求主人明确。 | 仅在精确检索不足时调用受控只读模型归纳，并持久化引用来源。 |

### P3：可延后验证与呈现（3）

| 根因 | 受影响条款 | 证据与风险 | 最小修正方向 |
|---|---|---|---|
| Host Bridge 双平台证据缺失 | B-02 | 本次仅静态核对，未在 macOS/Linux 各跑一次真实 CLI。 | 在隔离环境执行不付费的 process/session/output 契约测试。 |
| UI 观察标签与 Task 字段不完整 | B-13；Task 详情章节合同 | “最后 20 行”未始终明确标注非 Evidence，完整 Session/Artifact 不可直接打开。 | 统一 observation 标签并补 Worker/model/generation/Checker/Evidence/handoff 显示。 |
| live 部署链未在本次审计触碰 | B-03/A-07/E18 | 静态证明默认端口、单循环和 lease；未验证实际镜像、持久卷、Bridge token 与 production 单例。 | 在另一次获授权的只读/隔离验收中保存镜像、进程、端口、lease 和 Bridge probe Evidence。 |

## 6. 建议的验收顺序

1. 先写 characterization tests 固定当前 SQLite 数据兼容性，不新增公开状态或重复对象。
2. 将 formal message 统一改为 `PlannerResult → lifecycle reducer`，用结构化 schema 覆盖 size、A/B、客观选择条件、Task 原子性、result contract、DAG、Sprint、runtime。
3. 把 execution/verification/provider/cronner 中的 Task/Goal SQL 改为 facts，由唯一 reducer 事务化推进。
4. 建立完整结果 manifest：Artifact/Evidence/diff/session segment 都有 path、SHA-256、revision、范围、source Task；所有 tail 仅为 observation。
5. 统一模型产出 Checker，随后修 timeout、generation、Cold archive、Token usage_kind、Secret/authorization 终态撤销。
6. 最后收敛 UI/配置/脚本 promotion，并在隔离的 macOS、Linux、Docker 环境做 live 验收。

建议验证方式：

- 为 formal simple/medium/large 各提交一条指令，断言三者都产生真实 Planner HostJob 与可校验 PlannerResult，且启发式不决定最终 size。
- 用 SQL authorizer/代码边界测试禁止 lifecycle 以外模块更新 Goal/Task 生命周期列。
- 生成超过 1 MiB 的审查 Artifact，验证下游与 Checker 按 manifest 覆盖全文件，故意篡改任一 chunk/hash 必须 fail closed。
- 模拟 Worker/Planner/probe 模型产出，断言全部进入独立 Checker，Checker prompt 中不存在执行 Worker 自由聊天。
- 模拟 deadline、SIGTERM grace、SIGKILL、晚到结果、Provider failure 与 restart，逐步核对 HostJob、Artifact、Evidence、handoff、generation、Task 终态。
- 提交 cumulative usage 快照序列和跨 generation 序列，核对 raw snapshot、相邻 delta、Checker 分项及 Task 总预算。
- 向 message、模板、Prompt、日志和 env 注入测试 Secret 标记，断言 SQLite/文件/输出均不含明文，Task 终态后引用不可用。
- 取消 active provider_probe/git_publish Task，断言所有 HostJob 先终止且授权失效，再出现 cancelled outcome。

## 7. 机械统计与审计结论

| 章节 | 条款数 | 已实现 | 部分实现 | 仅文档 | 未实现 | 与基线冲突 |
|---|---:|---:|---:|---:|---:|---:|
| P | 15 | 4 | 5 | 0 | 1 | 5 |
| L | 6 | 4 | 0 | 0 | 0 | 2 |
| O | 17 | 16 | 1 | 0 | 0 | 0 |
| S | 6 | 6 | 0 | 0 | 0 | 0 |
| A | 25 | 18 | 3 | 0 | 0 | 4 |
| R | 17 | 5 | 4 | 0 | 1 | 7 |
| M | 17 | 12 | 2 | 0 | 0 | 3 |
| T | 10 | 6 | 0 | 0 | 0 | 4 |
| D | 30 | 19 | 3 | 0 | 1 | 7 |
| B | 21 | 11 | 8 | 0 | 0 | 2 |
| C | 4 | 1 | 1 | 0 | 0 | 2 |
| **合计** | **168** | **102** | **27** | **0** | **3** | **36** |

- 覆盖条款：**168 / 168**；无遗漏、无重复、无额外条款 ID。
- P0 合并根因：**6**
- P1 合并根因：**6**
- P2 合并根因：**4**
- P3 合并根因：**3**

因此本仓库已经具备若干可复用的 V2 原语，但尚未满足唯一基线的主线、生命周期单写者、完整结果和独立验收硬合同，审计结论为 **不通过**。修正时应保留四态、现有核心表、Task/TaskSession/Generation、Artifact/Evidence、Cronner、三层记忆和 Provider 有序递补等已实现能力，不应通过新增重复状态机或第二推进器绕过根因。

## 8. 未运行事项与最终 Git 状态

未运行：

- 全量测试或与本审计无关的测试。
- Docker build/up、蓝绿部署、切流、生产端口/卷/镜像检查。
- 真实 Host Bridge start/read/stop、真实 Codex/Cursor/DeepSeek/Kimi 调用。
- 外部付费 Provider、网络、远端 Git、外部发送/发布。
- 对任何现有 SQLite、任务、配置、workspace、服务或 Git 历史的写操作。

报告完成后的实际 Git 状态：

```text
## main
?? docs/BASELINE_V2_CONFORMANCE_AUDIT.zh-CN.md
```
