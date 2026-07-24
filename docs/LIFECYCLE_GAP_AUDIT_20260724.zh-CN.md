# PlowWhip Web 生命周期差异审计

- 日期：2026-07-24
- 目标：以主人给出的 Mermaid 生命周期为唯一预期，核对当前 clean-room V1
- 方法：独立 Sol 极高只读审计；源码、Schema、测试和 8750 现场状态交叉核验
- 结论：主干闭环已经存在，但尚不能宣称该生命周期完整实现。共有 `P0 × 4`、`P1 × 3`、`P2 × 2`、`P3 × 1`。

## 已符合的主干

1. Cronner 是应用内唯一物理定时器，持项目租约后调用 `advance_project`。
2. Provider、Planner、Checker 外部 I/O 已移出 SQLite 写事务。
3. Task 公开状态保持 `Pending / InProgress / NeedsDecision / Done` 四态。
4. 模型 Task 有独立 Checker；确定性 Task 使用零模型验证。
5. TaskSession 以 `project + role + task` 为物理会话边界，同 Task 修复可续用。
6. Monitor 使用只读连接，并默认只展示最新 20 行。
7. Hot / Warm / Cold、Provider 原生 compact、Evidence 和 Artifact 均已有真实消费者。
8. 8750 现场 Goal `064962c153a9441fb426d7b2ff6b2473` 已实际拆成 Git、Cursor、Codex 三个串行 Task，而不是一个复合 Task；三个 Task 均已完成。

## P0：会产生错误业务结论

### LIF-P0-01：新消息会抢在当前 Goal 的 queued DAG 前面

- 预期：一个项目只有一个活动 Goal；新 Goal 排队，除非主人明确插队。
- 当前：当前没有到期活动 Task 时，生命周期先消费未处理普通消息，再激活已选 Plan 中 ready 的 queued Task。
- 证据：`plowwhip/lifecycle.py:223-232` 位于 queued dependency/ready 处理 `plowwhip/lifecycle.py:234-285` 之前。
- 已有反例测试：`tests/test_vertical_slice.py:932-975` 明确期待紧急新消息在第二个 Plan Task 前 intake，并最终得到三个 Task；这把错误顺序固化成了测试合同。
- 风险：同一项目的 Goal DAG 被普通新指令穿插；Provider、上下文和验收归属不再是严格串行。
- 最小修复：先选定唯一活动 Goal；只在其 DAG 终态后 intake 下一普通消息。插队必须是带目标 Goal/Task 和 revision 的显式 Action。

### LIF-P0-02：Goal 验收没有覆盖到 Plan/Task 验收

- 预期：GoalSpec 的每条验收都必须被选中 Plan 的 Task acceptance 覆盖；Goal Done 必须有当前 revision 的完整 Evidence。
- 当前：Planner 只校验每个 Task 自己有 1–20 条 acceptance，没有保存 `goal_acceptance_id → task_acceptance_id` 映射，也没有 Goal 完成门禁。
- 证据：`plowwhip/planner.py:215-365` 只归一化 alternatives、Task DAG 和 Task acceptance；`plowwhip/lifecycle.py:2148-2315` 直接安装 Task；`plowwhip/monitor.py:593-628` 仅按 Task 计数推导 Goal 状态。
- 风险：Planner 可以生成与 Goal 无关但都能 PASS 的 Task，最后仍把 Goal 显示为 Done。
- 最小修复：为 Goal acceptance 分配稳定 ID；Plan 必须提交覆盖映射；安装 Plan 前做确定性全集/无悬空校验；Goal reducer 只接受当前 Goal/Plan revision 的完整 Evidence。

### LIF-P0-03：普通“决定”会整体覆盖冻结 TaskSpec

- 预期：项目管家只问一个具体问题；回答只修改该问题允许的字段。
- 当前：除少数特判外，`provide_decision` 会把主人回答重新送入 `normalize_instruction`，然后整体替换 `spec_json` 和 `acceptance_json`。
- 证据：`plowwhip/lifecycle.py:1090-1140`。
- 风险：回答“允许”“继续”或业务选项可能意外改写 Provider、写范围、验收和 Task 目标；Goal/Plan revision 也没有成为通用决定的绑定条件。
- 最小修复：NeedsDecision 持久化 `question_id / reason / allowed_actions / target_revision / patch_schema`；回答只能应用类型化 patch，禁止重新解释成完整新任务。

### LIF-P0-04：取消 Task 可以让 Goal 被聚合成 Done

- 预期：只有验收 Evidence 齐全才允许 Done；取消不是完成。
- 当前：取消把 Task 写成 `public_status=done, outcome=cancelled`；Goal 汇总把所有 `outcome IS NOT NULL` 都计为 terminal，在没有 pending/in-progress/needs-decision 时显示 Done。
- 证据：`plowwhip/lifecycle.py:622-632`；`plowwhip/monitor.py:599-628`。
- 风险：零 Evidence 的取消 Goal 被 UI 宣称完成，直接违反 `PASS → DONE` 门禁。
- 最小修复：Goal reducer 区分 `done / cancelled / failed`；只在所有 required Task `outcome=done` 且 Goal acceptance Evidence 完整时显示 Done。

## P1：破坏职责边界或无人值守

### LIF-P1-01：Planner 借用首个业务 Task 占位

- 当前：大型 Goal 先创建一个 `phase=plan` 的 Task；安装 Plan 时把该 placeholder 原地改写为 Plan 第一个业务 Task。
- 证据：`plowwhip/lifecycle.py:2178-2215`。
- 影响：Planner 控制面运行与第一个业务 Task 共用 Task 身份和部分历史，审计时难以区分“形成计划”和“执行业务”。
- 最小修复：Planner 作为 Goal/Plan revision 的控制面运行记录，不占业务 Task；授权 Plan 后一次性创建全部业务 Task。

### LIF-P1-02：报告工件异常直接升级给主人

- 当前：报告缺失、截断、读取失败、SHA/字节不匹配时，Checker 不启动，Task 直接进入 NeedsDecision。
- 证据：`plowwhip/verification.py:407-445`、`plowwhip/verification.py:1064-1085`。
- 影响：已有成功 HostJob 可恢复、可重新物化报告或可有界重试 Checker 时，仍不必要地打断无人值守。
- 最小修复：先做零 Token 同源 HostJob 报告恢复；再做一次有界 Checker 合同重试；仍失败才生成带原因和可选动作的 NeedsDecision。

### LIF-P1-03：SimpleWorker 没有通用确定性命令消费者

- 当前：简单任务只有 `write_text`、Provider 探针和专用 Git Publisher；资源库虽然有 `script` 类型，但没有从 TaskSpec 到白名单脚本/命令的执行合同。
- 证据：`plowwhip/planner.py:79-80`、`plowwhip/intake.py:915`、`plowwhip/lifecycle.py:2480`、`plowwhip/store.py:208`。
- 影响：本应零模型完成的简单命令被迫进入专业模型 Worker，增加 Token、时延和失败面。
- 最小修复：只增加一个确定性 `command` TaskSpec：资源库脚本 ID、固定 argv、绑定 cwd、timeout、允许的退出码和输出文件哈希；禁止任意 shell 字符串。

## P2：产品体验或性能边界不完整

### LIF-P2-01：全局管家只路由，未在原窗口转接到项目管家历史

- 后端已经能精确定位项目并保存路由引用。
- 前端提交后只设置 `currentProject`、刷新项目和调用 `loadButler()`，没有切换到项目管家视图。
- 证据：`plowwhip/butler.py:120-191`；`plowwhip/ui.py:351`。
- 最小修复：路由成功后在同一窗口切换到项目管家，并定位到该项目历史；不创建第二份会话。

### LIF-P2-02：连续性 checkpoint 在 SQLite 写事务内扫描全部历史 Session 并做文件 I/O

- 当前：`checkpoint_project` 打开写事务，查询项目所有历史 TaskSession，然后在事务内读取和写入 segment/handoff 文件。
- 证据：`plowwhip/continuity.py:13-35` 及其 `_segment_session`、`_checkpoint_session` 文件操作。
- 影响：项目历史增长后会延长写锁，拖慢 API 与 Cronner；也会扩大 checkpoint 局部失败的影响面。
- 最小修复：只处理本 Tick 受影响的 TaskSession；事务内冻结结构化清单，事务外原子写文件，再用短事务登记哈希。

## P3：信息架构缺口

### LIF-P3-01：Sprint 已存储但 Task 页面没有按 Sprint 分组

- 当前：Task 详情显示 Sprint 数字，泳道仍只按四个公开状态平铺。
- 证据：`plowwhip/monitor.py:546-590` 返回 sprint；`plowwhip/ui.py:252` 仅按状态分泳道；`plowwhip/ui.py:267` 只在详情显示 Sprint。
- 最小修复：保持四态为列，在列内按 Sprint 折叠分组；不增加第五种 Task 状态。

## 推荐实施顺序

1. 先修 `P0-01 → P0-04 → P0-02 → P0-03`，建立唯一活动 Goal、正确终态和验收覆盖。
2. 再拆 Planner 控制面身份，并补报告自动恢复；这两项决定大型 Goal 能否真正无人值守。
3. 增加一个受限 SimpleWorker command 合同，删除可以被它替代的模型路由。
4. 最后完成管家原窗口转接、checkpoint 缩短写锁和 Sprint 分组。

每一项必须有最小反例测试。不得用“测试总数通过”、Provider 退出码、Task heartbeat 或 UI 显示 Done 代替 Goal acceptance Evidence。
