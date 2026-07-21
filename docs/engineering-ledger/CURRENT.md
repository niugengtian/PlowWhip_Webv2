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
