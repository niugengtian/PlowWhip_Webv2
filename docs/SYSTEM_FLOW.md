# Plow Whip Web V2 系统流转

本文描述当前实现的统一主链。状态、续接、验证和告警必须落在这条链上，不为单个故障增加旁路状态机。

## 1. 从指令到完成

```mermaid
flowchart TD
    H["主人 / 外部 Agent"] --> S{"选择作用域"}
    S -->|全部项目| GB["全局管家<br/>Codex 独立会话"]
    S -->|具体项目| PB["项目管家<br/>Codex 项目隔离会话"]
    GB --> GQ["只读查询全部工作区<br/>Goal / Task / Worker / Provider / Alert"]
    GB --> GR["引导主人切换到目标项目"]
    GR --> PB
    PB --> C{"目标、边界、验收标准<br/>是否足够明确"}
    C -->|L / XL 且不明确| Q["一次只问一个问题"]
    Q --> C
    C -->|明确| P["生成方案和 Task DAG"]
    P --> HC{"主人确认"}
    HC -->|修改| Q
    HC -->|确认| F["冻结 GoalSpec / TaskSpec<br/>ProviderPolicy / RuleSnapshot"]
    F --> RI["匹配数据库模板<br/>创建 RoleInstance"]
    RI --> D["按依赖和资源锁派发"]
    D --> E["Execution Episode"]
    E --> V{"候选实现门禁"}
    V -->|CHANGES_REQUIRED| RP["同一 Task 新修复周期<br/>Attempt + 1"]
    RP --> E
    V -->|candidate_ready| IV["全新 verification Task<br/>独立 Session 只读复验"]
    IV -->|CHANGES_REQUIRED| RP
    IV -->|PASS| DONE["Goal completed"]
```

- 全局管家不替项目执行；项目之间的会话、规则和工作区隔离。
- 同一条 intake 对话绑定一个物理管家 Session；确认生成 Goal 后归档，新指令创建新对话。
- `project_id + role_id + task_id` 三者相同才允许复用物理 Worker Session。
- 实现任务只能到 `candidate_ready`；只有全新独立验证任务的结构化 `PASS` 可以完成 Goal。

## 2. 调度、网络和 Provider

```mermaid
flowchart TD
    T["Ready Task<br/>冻结 ProviderPolicy"] --> N{"网络分区健康"}
    N -->|大陆区与海外区均失败| NS["network_suspended<br/>暂停全部进行中任务"]
    NS --> NR{"每个分区连续 3 次恢复"}
    NR -->|否| NS
    NR -->|是| RB["按批次恢复"]
    N -->|至少一个分区可用| PS["按冻结顺序选择 Provider"]
    PS --> P{"Provider 探测"}
    P -->|成功| HB{"Host Bridge 就绪"}
    P -->|连续 3 次失败| OPEN["Provider Circuit OPEN"]
    OPEN --> POL{"Policy"}
    POL -->|auto / preferred 且允许 fallback| NEXT["切换下一个 Provider<br/>新 Session generation"]
    NEXT --> P
    POL -->|pinned 或禁用 fallback| PSS["provider_suspended"]
    PSS --> HUMAN["人工批复：继续一次 / 切换 / 换会话 / 取消"]
    HB -->|失败| SUSP["provider_suspended 或 needs_human"]
    HB -->|成功| RUN["启动 Episode"]
```

- 大陆网络区：DeepSeek、Kimi；海外网络区：Codex、Cursor。
- 每区使用 DNS 与两个端点交叉探测；两个区都失败才认定全局断网。
- 断网、Provider、Host Bridge 和 watchdog 故障属于基础设施状态，不写成业务 `CHANGES_REQUIRED`。

## 3. Attempt、Episode、断点续接与 Watchdog

```mermaid
stateDiagram-v2
    [*] --> Ready
    Ready --> Running: 创建 Attempt 1 / Episode 1
    Running --> Running: 网络或 Provider 恢复\n同 Attempt 新 Episode
    Running --> Suspended: network/provider/bridge 故障
    Suspended --> Running: 自动恢复或人工 grant\n从 checkpoint 最小检测
    Running --> CandidateReady: 实现 Gate 通过
    CandidateReady --> Verifying: 全新 verification Task
    Verifying --> Completed: PASS
    Verifying --> Ready: CHANGES_REQUIRED\n新修复周期 Attempt + 1
    Running --> NeedsHuman: 预算耗尽或不可恢复
    NeedsHuman --> Running: 人工批复续接
    NeedsHuman --> Cancelled: 人工取消
```

续接只加载不可变 TaskSpec/规则/Provider 策略、最新结构化 checkpoint、当前工作区和已验证 Artifact 的路径/哈希/revision、针对断点的最小检测及下一个动作。不会重放旧聊天、完整日志或完整终端输出，也不会因 `run_id` 改变要求无意义改写文件。

Watchdog 按 `人类 Task+角色约定 > 项目设置 > 全局设置` 解析有效阈值并记录来源。Episode wall、checkpoint、无进展、进展续期和最大 Host 进程共同受 Task hard deadline 约束；heartbeat 或输出字节本身不算进展。

## 4. 证据与终态

```mermaid
flowchart LR
    CMD["真实 argv + cwd"] --> EXIT["退出码 + 起止时间"]
    EXIT --> ART["产物路径 + SHA-256"]
    ART --> ACC["逐条验收项"]
    ACC --> MAN["EvidenceManifest<br/>Task / Attempt / Episode / Session"]
    MAN --> VER{"结构化 verdict"}
    VER -->|PASS| PASS["passed=true"]
    VER -->|CHANGES_REQUIRED| FAIL["passed=false"]
```

空 command、泛化 exit code、模型声明、heartbeat、queued、accepted 或 `wake_accepted` 均不能证明完成。EvidenceManifest 追加写入；更正通过 `supersedes` 关联旧记录。`CHANGES_REQUIRED` 不能生成 `passed=true`。

## 5. 告警收敛

```mermaid
flowchart TD
    RAW["原始探测 / Provider / Task 事件"] --> CORR["根因关联器"]
    CORR --> G{"是否全局断网"}
    G -->|是| ONE["单一全局网络 Incident"]
    ONE --> SUP["抑制 Provider / Task 派生告警"]
    G -->|否| Z["按网络区或 Provider 聚合"]
    Z --> INC["单一 scope Incident<br/>计数 + 最近时间"]
    SUP --> DB["原始事件仍追加保存"]
    INC --> DB
    DB --> UI["告警中心 / 管家查询"]
```

告警使用 debounce、失败滞后和恢复滞后；重复事件只增加 occurrence count，不制造告警风暴。

## 6. Token 账本与界面

```mermaid
flowchart LR
    SNAP["物理 Session 累计快照"] --> DELTA["相邻快照差分"]
    DELTA --> LEDGER["ModelCallLedger<br/>原始快照 + 规范化增量"]
    LEDGER --> DAY["Asia/Shanghai 日聚合"]
    DAY --> GLOBAL["全部项目曲线"]
    DAY --> PROJECT["项目曲线"]
    DAY --> PIE["项目 / Task 饼图"]
    GLOBAL --> AVG["各系列完整历史日均值<br/>每日 00:00 重算"]
    PROJECT --> AVG
    AVG --> COLOR["紫靛蓝绿黄橙红相对量级"]
```

`cached_input_tokens` 是 `input_tokens` 的子集；Total 为 Input + Output，不重复相加 Cached。动态颜色只与各系列自己的完整历史日均值比较，当前未结束的上海自然日不进入基线。
