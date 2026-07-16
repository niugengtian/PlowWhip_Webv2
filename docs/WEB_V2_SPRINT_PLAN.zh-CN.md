# plow-whip Web v2 Sprint 执行计划

> 状态：待用户审阅
> 日期：2026-07-16
> 产品目标：保障质量的前提下实现无人值守完成，尽量减少 Token 消费。
> 执行约定：本计划一旦整体通过，后续 Sprint 连续执行，不再逐 Sprint 等待人工确认；只在不可替代的产品决策、外部凭据、系统权限或发布动作前停止请求用户。

## 1. 当前真实基线

- 当前仓库主体仍是旧版 Python CLI 协作系统，尚无 FastAPI、React/Vite 或 SQLite Event Store 的 Web v2 实现。
- 当前分支 `plow/t-20260715033340177720-5b09afb82a` 存在大量未提交的旧系统改动；它们属于既有工作，不能覆盖、清理或假装由 Web v2 创建。
- 现有 `pyproject.toml` 运行依赖只有 `httpx`；`pytest` 未安装。
- 现有基线使用标准库测试运行：`python3 -m unittest discover -s tests -q`，结果为 229 项通过。
- 已审阅产品基线：`docs/PLOW_WHIP_WEB_V2_PRODUCT_REVIEW.html`。
- 详细设计基线：`docs/WEB_V2_DESIGN.zh-CN.md`。

## 2. Sprint 总原则

### 2.1 不是三个并列目标

```text
保障质量（前提）
  └─ 实现无人值守完成（目标）
       └─ 尽量减少 Token 消费（全过程优化）
```

发生冲突时：

1. Token 优化不得跳过测试、Verification、权限检查或完成证据。
2. 无法保证质量时，无人值守系统必须停止到明确状态，不能猜测完成或无限重试。
3. 能由 Python、数据库、PID、文件、网络探针或本地测试确定的操作，不允许调用模型。
4. 只有明确的 Plan/Execute/Review Model Run 可以产生 Token 消耗。

### 2.2 Sprint 是验收边界，不是日期承诺

每个 Sprint 必须交付一条可运行的纵向能力，并通过自动测试和演示证据。退出门未通过时，只在本 Sprint 内修复，不带病进入下一个 Sprint。

### 2.3 新旧系统隔离

新版不是旧仓库里的新 Python 包，也不是 Git worktree。它必须是完全独立的同级目录和独立 Git 仓库：

```text
/Users/niugengtian/work/
├── plow-whip/              # 旧项目：只读参考，不再写入
└── plow-whip-web-v2/       # 新项目：独立 Git、依赖、数据和 Scheduler
    ├── backend/
    │   └── plow_whip_web/  # FastAPI、Domain、Store、Runtime、Provider
    ├── web/                 # React + TypeScript + Vite
    ├── tests/               # 单元、集成、故障注入、E2E
    ├── docs/                # 已审阅设计、Sprint 和运行手册
    └── runtime/             # 新版本机数据，Git ignore
```

硬隔离规则：

- 新目录拥有自己的 `.git`、`pyproject.toml`、`package.json`、虚拟环境、SQLite、日志、Archive 和 Scheduler definition。
- 不使用旧仓库的 Git worktree、submodule、软链接、运行目录、数据库、`collab/` 或系统任务。
- 新代码不得 `import plow_whip`；需要保留的理念重新按 Web v2 领域模型实现。
- 旧 `/Users/niugengtian/work/plow-whip` 只读参考；不在其中实现、测试、安装或生成新版运行数据。
- 已审阅的产品、设计和 Sprint 文档在 Sprint 0 复制到新仓库，之后以新仓库版本为真源。
- 若未来需要读取旧项目数据，只能通过显式、只读、可撤销的导入工具，不能形成运行时依赖。

## 3. Sprint 依赖关系

```text
S0 基线隔离与工程骨架
  → S1 质量优先的单任务闭环
    → S2 多项目与项目劳动力
      → S3 系统 Scheduler 与 Settings
        → S4 Context、Convention、Token 与轮转
          → S5 人工控制与故障恢复
            → S6 安全、完整 UI 与发布验收
```

每个 Sprint 内，Backend、Frontend、测试可以并行推进；领域状态机和数据库 Schema 是共同契约，必须先冻结再并行。

## 4. Sprint 0：基线隔离与工程骨架

### 目标

在全新目录 `/Users/niugengtian/work/plow-whip-web-v2` 建立独立仓库；旧项目保持只读和零运行耦合。

### 交付范围

- 创建新的 `/Users/niugengtian/work/plow-whip-web-v2` 目录并执行独立 `git init`，不使用旧仓库 worktree。
- 将已审阅文档复制到新仓库 `docs/`，记录来源，但不复制旧实现代码。
- 创建 `backend/plow_whip_web/`、`web/`、`tests/`，不复用旧状态机。
- 增加 FastAPI、SQLite、迁移、React/TypeScript/Vite 的最小工程配置。
- 建立应用配置 Schema、数据目录、日志和 Secret 引用约定。
- FastAPI `/health`、前端应用壳、SQLite WAL 初始化和幂等迁移。
- 建立 Backend 单元/集成测试与 Frontend lint/typecheck/test/build 命令。
- CI/本地统一质量命令；旧版 229 项测试结果只作为迁移前证据，不成为新版运行依赖。

### 退出门

- `git -C /Users/niugengtian/work/plow-whip-web-v2 rev-parse --show-toplevel` 只指向新目录。
- 新仓库没有旧 `.git`、worktree、submodule、软链接或 `plow_whip` 包依赖。
- 旧仓库执行前后的 `git status --short` 完全一致。
- 旧版 229 项基线测试结果已归档；新版拥有独立测试命令。
- 新 Backend 可启动，`/health` 返回版本、数据库和迁移状态。
- 新 Frontend 可构建并显示空控制台壳。
- 数据库初始化重复执行不改变已存在数据。
- Git diff、Python compile、Frontend typecheck/lint 无错误。
- 当前旧系统未提交改动未被覆盖、删除或混入新版仓库。

### 不做

- 不接真实模型，不启动 CLI Worker，不安装 OS Scheduler。
- 不把页面“能打开”当作任务闭环完成。

## 5. Sprint 1：质量优先的单任务最小闭环

### 目标

证明最小产品纵向链路：从 Web 创建 Task，经一次受控执行和确定性验证，进入不可逆的正确终态。

### 交付范围

- 核心对象：Task、Step、Attempt、Run、Event、Artifact、Evidence、Budget。
- Command Service 与纯确定性 reducer；所有状态写入携带 `expected_revision`。
- SQLite 事务、不可变 Event、当前投影、幂等命令键。
- 一个 Fake Provider 和一个 Generic Command Provider，用于无外部账号测试。
- Tool Runner：目录白名单、命令白名单、超时、输出上限。
- Verification Engine：退出码、文件、Schema、HTTP 和用户命令断言的最小集合。
- Economy 流程：`create → ready → run → verify → completed/terminal_failed/needs_human`。
- Task 列表、创建页、Task Detail、状态时间线、Artifact/Evidence 展示。
- 完成吸收态、预算硬停止、没有新 Evidence 不得再次 Run。

### 退出门

- 一个真实临时项目可从 Web 无人工接力地完成一个可验证任务。
- 模型/Provider 自称完成但 Verification 失败时，Task 不能完成。
- `completed/terminal_failed/cancelled` 不能被后台重新激活。
- 同一 Task 同时最多一个 active Attempt。
- 重复提交同一 Command 不产生重复 Run 或 Event。
- 目录、权限、凭据和验证配置错误在 Model Run 前发现。
- Economy 成功路径最多一次 Model Run。

### 覆盖原验收

1、2、4、6、7、8、10。

## 6. Sprint 2：多项目并行与 Project-Role-Worker 劳动力

### 目标

让多个项目真实并行，同时稳定维持 `Project → Role → CLI Session` 绑定，并保证一个打工仔一次只做一个 Task。

### 交付范围

- Project、Role、CliProvider、RoleWorker、SessionArchive、ExecutionLease、ResourceLock。
- Web3/IT 最小 5 岗位：协调负责人、全栈工程师、Web3 工程师、DevOps/SRE、审查与验证。
- 低频能力使用 Task Capability Pack，不新增常驻角色。
- Generic CLI Provider Adapter 协议：hire/resume/capture/rotate/release/probe process。
- Global Dispatcher、项目队列、round-robin 公平扫描和有界 Worker Pool。
- 全局、项目、Provider 三层并发限制；仓库/worktree/端口等资源锁。
- Role Worker generation、Session ID、PID、active_task_id 和 CAS。
- Task 完成只释放 Execution Lease，Worker 回到 ready；Project 终态执行 Release Barrier。
- Projects 页面和单项目 Workforce 页面。

### 退出门

- 两个不同 Project 可真实同时推进；一个项目暂停、失败或预算耗尽不阻塞另一个。
- 同一 Worker Session 永不同时持有两个 Task。
- 同一 Project Role 只有一个 active generation。
- 资源冲突任务不会同时执行。
- Project 完成/取消/归档后无 Worker PID、有效 Lease、临时 Secret 或可 Resume active binding。
- 连续 Task 默认复用同一 Role Worker；跨项目绝不复用。

### 覆盖原验收

13、14、19、20、21、28、29。

## 7. Sprint 3：单一系统 Scheduler、0 Token Tick 与 Settings

### 目标

浏览器和 Web 服务关闭后，操作系统仍能用一个高可用 Scheduler 扫描全部项目并确定性推进，不产生模型探活 Token。

### 交付范围

- Python Runtime `tick` 单次命令：短进程、可重入、幂等、有界批次。
- SchedulerLease、fencing token、heartbeat、扫描游标、幂等派发键。
- 固定扫描矩阵：Project、Role、CLI、Worker、Session、Task、Lease、Rotation、Circuit。
- macOS launchd、Linux systemd user/cron、Windows Task Scheduler 的能力探测和定义生成器。
- 安装计划、Test、Repair、Disable、Uninstall；实际系统变更必须经 Web 明确授权。
- Settings Schema 与 Set 页面：General、Runtime、Projects、Workforce、Providers、Scheduler、Rotation、Token、Automation、Permissions、Storage、Health。
- 设置 revision、影响预览、Audit Event、Secret 引用不回显。
- 控制面关闭、SSE 断线、Backend 重启不改变任务真相。
- 0 Token Event 计量：Probe/Wake/Dispatch/Drive 不得创建 Model Run。

### 退出门

- 重叠 Tick 中只有持有最新 fencing token 的实例可写状态或启动 Worker。
- Tick 在任意崩溃点重启后不产生双 Worker和重复 Model Run。
- 单次 Tick 输出扫描计数、游标、Claim、跳过原因和耗时。
- 0 Token 路径测试断言 Model Gateway 调用次数为 0。
- 三种 OS Adapter 均有确定性生成/解析测试；当前 macOS 可完成 plan/test，安装必须由页面授权。
- Settings 非法值、旧 revision 和越权修改全部拒绝。

### 覆盖原验收

3、15、16、17、18、24、25、27。

## 8. Sprint 4：Context、Convention、Token 预算与原子轮转

### 目标

在不降低质量和无人值守能力的前提下，把模型上下文和调用次数压到最小，并保证 Session/文件可轮转、可恢复、可审计。

### 交付范围

- Context Compiler：Goal、当前 Step、Acceptance、Artifact、最新 Evidence、Budget。
- Global / Project / Task 三种 Convention 作用域；稳定规则 ID、触发条件、`on_violation`。
- 每次 Capsule 最多注入 8 条相关规则；完整规则不重复进入 Prompt。
- 5 个紧凑 Web3/IT 角色模板；参考 agency-agents-zh 的结构，不复制全量角色库。
- Task/Project/日预算、Run Token 计量、Provider 成本、缓存 Token、剩余预算和硬停止。
- Usage 页面：真实 Task 总 Token、重试成本、本地 Verify 替代量、重复上下文避免量。
- Session 软/硬阈值、文件 bytes/消息块阈值、Cold Archive、SHA-256 和 archive pointer。
- 确定性 Carry Forward，不调用模型总结；轮转 successor generation 原子接管。
- Provider/CLI/Session/Role 切换不重置 Attempt、Token 或成本预算。

### 退出门

- Context Capsule 在正常/硬上限内，超限有确定性裁剪原因。
- 相同输入生成稳定 Convention hash 和 Capsule hash。
- 轮转失败可重入，不重复归档同一 source hash。
- 旧 generation 的迟到结果被拒绝。
- 完整消息块/行不会从中间切断。
- 每笔 Token 都关联明确 `model_run.started`；系统控制事件 Token 为 0。
- 相同验收任务可对比旧版基线的总 Model Runs、输入重复量和最终通过率；不预设虚假节省百分比。

### 覆盖原验收

5、9、16、22、23、27。

## 9. Sprint 5：人工控制、连接异常与可靠恢复

### 目标

系统能在崩溃、休眠、断网、Provider 故障和数据库抖动后继续正确推进；不能继续时正确停止并让人直接决策。

### 交付范围

- pause/resume/cancel、Goal 修订、Decision Panel、人工增补预算。
- Outbox、SSE sequence/replay、通知失败隔离。
- 进程崩溃恢复、失效 Lease 回收、PID/进程组 TERM→有界等待→KILL。
- Circuit Breaker：Global、Project、Role、Provider、国内链路、海外链路。
- 国内断网、海外断网、飞行模式、休眠唤醒、SQLite lock/disk 抖动的确定性分类。
- missed ticks 合并为一次 resume reconcile，不逐个回放。
- 基础设施失败不消耗模型 Attempt；half-open 只允许单个 0 Token 探针。
- 相同 `failure_class + evidence_hash` 去重；修复、Review 和 Provider failover 共享 Task 总预算。
- Health、Needs You、Audit 和异常下一动作页面。

### 退出门

- 浏览器关闭、SSE 断开和通知失败不影响 Task 终态。
- 休眠恢复不会回放全部旧 Tick，不出现并发恢复风暴。
- 单 Provider、Role 或 Project 熔断时，其余项目继续扫描。
- 无新 Evidence、人工决策或到期 retry_at 时，不创建新 Model Run。
- 连续 100 个崩溃、超时、重复请求、锁竞争、旧 revision、断网和验证失败注入：零重复 Model Run、零越过终态、零无限循环、零双 Worker。

### 覆盖原验收

2、3、5、11、12、25、26。

## 10. Sprint 6：安全边界、完整产品面与发布验收

### 目标

把已证明的运行闭环收敛成可安装、可操作、可审计、可维护的本机 Web 产品，并完成全量发布验收。

### 交付范围

- Tool/Network/Secret/Project Root 权限策略与审批记录。
- Git diff/worktree、文件 Artifact、验证命令和 Provider 权限策略。
- loopback 默认监听；非 loopback 时启用本地认证、Origin/CSRF 防护。
- 完成 Today、Projects、Project Workforce、Tasks、Task Detail、Usage、Providers、Audit、Health、Settings 页面。
- Balanced 一次规划和 Strict 一次独立 Review；Review 固定上限，最多一次裁决。
- 备份、导入导出、诊断包和数据库恢复演练。
- 本机一键启动、开发/生产构建、升级迁移和卸载说明。
- 完整 API/OpenAPI、架构、运行手册、故障手册和安全边界文档。
- 全量单元、集成、前端、E2E、故障注入和安装包验证。

### 退出门

- 详细设计中的 29 条验收标准全部有自动证据或明确的人机授权证据。
- 一个 Web3 示例项目和一个普通 IT 示例项目可并行完成，并通过独立 Verification。
- 真实 CLI Provider 缺失时系统明确阻塞，不伪装完成；Fake/Generic Provider 的 E2E 不依赖厂商 Agent Runtime。
- 权限越界、跨项目读取、Secret 回显、旧 revision 和重复 Command 测试全部拒绝。
- 全量测试、编译、类型检查、lint、构建、迁移、故障注入通过。
- 发布说明明确 MVP 不承诺 SaaS、多机集群和全 CLI 兼容。

### 覆盖原验收

全部 1–29 的最终回归。

## 11. 全程硬质量门

每个 Sprint 都必须保存以下证据：

1. 需求/验收到测试用例的映射。
2. 变更文件与数据库迁移清单。
3. 单元、集成、E2E 和失败路径结果。
4. 状态不变量与并发测试结果。
5. Token/Model Run 计量；无模型路径必须证明调用次数为 0。
6. 安全与权限变化说明。
7. 已知限制和未完成项；不允许用“以后补”冒充完成。
8. Sprint 退出门结论：`pass` 或 `blocked`，不能用模糊百分比。

实现过程中禁止：

- 为了赶进度跳过 Verification。
- 用模型判断 Scheduler、Lease、PID、网络或状态迁移。
- Provider/Session 切换后重置预算。
- 为修复异常无上限增加角色、Review 或重试。
- 修改或清理用户现有未提交工作。
- 在旧 `/Users/niugengtian/work/plow-whip` 内实现、运行或存储 Web v2。
- 通过 Git worktree、submodule、软链接、Python import 或共用数据库把新旧系统重新耦合。
- 未经授权安装系统任务、推送 Git、发布版本或传输 Secret。

## 12. 批准后的连续执行规则

用户整体批准本计划后：

1. 从 Sprint 0 开始连续推进到 Sprint 6，不再逐 Sprint 请求批准。
2. 每个 Sprint 退出门通过后自动进入下一个，并写入证据报告。
3. 可自动修复的测试失败、实现错误和兼容问题由执行者自行修复。
4. 以下情况才进入 `needs_human`：
   - 需求存在两种会显著改变产品行为的合法解释；
   - 缺少不可替代的账号、Secret 或外部服务授权；
   - 需要真实安装 OS Scheduler、提升权限、推送或发布；
   - 用户现有改动与新版必须修改的同一位置发生无法安全合并的冲突。
5. 阻塞时保存完整状态、最短 Evidence 和唯一下一动作；恢复后不重放完整历史。

## 13. 本次审阅只需要确认的事项

请重点确认：

- 是否接受 7 个 Sprint（S0–S6）的依赖顺序。
- 是否接受新根目录固定为 `/Users/niugengtian/work/plow-whip-web-v2`，并与旧仓库完全独立。
- 是否接受批准后连续执行，只在权限、凭据、发布和不可替代产品决策时打断。
- 是否接受 Sprint 6 才做完整发布收敛，前面每个 Sprint 都保持可运行、可验证。

整体通过口令：`Sprint 计划通过，开始连续执行`。
