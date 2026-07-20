# 两级管家契约

## 职责边界

全局管家是零模型 Token 的只读索引和路由器。它只查询已注册的 Project、
Goal、Task、Worker 规范状态；`workspace_root` 只用于过滤注册路径，不触发目录
遍历或文件读取。全局管家不拥有项目会话，路由成功后调用方直接使用返回的
`direct_project_butler_url` 与项目管家交互。

项目管家是逻辑常驻、项目隔离的入口，不等于一个跨 Task 永久 Provider 会话。
每次 intake 会话有自己的 revision、消息序号、服务器计算的 confidence 和
proposal hash。实际 Worker 的物理会话仍绑定 `project + role + Task`。
Web 将消息历史、当前问题、方案及修订都显示在同一个项目管家聊天面板中；
关闭页面后可从项目会话列表恢复，不使用 `window.prompt/window.confirm`
模拟对话。

**规则作用域（不可混淆）：**

- **四原则（Think Before Coding / Simplicity First / Surgical Changes /
  Goal-Driven Execution）仅属于开发角色**：`backend`、`frontend`、`ui`、
  `fullstack`、`devops_sre`、`verification`，以及明确承担实现/审查/验证的
  `capability:*` Worker。原则内容来自数据库 `rule_versions`（经 RoleInstance
  快照注入 Context）；非开发角色预览显示「不适用」，不是「被裁剪」。
- **95% 语义澄清（一次一问、置信门、主人确认）仅由项目管家执行**。Worker
  不承担需求澄清；全局管家不代替项目管家做 95% 澄清。
- **全局管家模板独占只读跨项目索引与路由**；不共享项目 Session，不跨项目注入
  Context。

## Intake 状态机

```text
instruction
  -> planning(项目管家 Provider)
  -> clarifying(仅在模型识别出真实信息缺口时一次一个)
  -> awaiting_confirmation(confidence=95, proposal_hash)
  -> dispatched(goal_id)

planning
  -> provider_suspended(Provider 不可用或契约无法收敛)
  -> planning(人类恢复 Provider 后续接同一会话)
```

- 自然语言指令先交给项目管家 Provider 理解；服务端不再用固定字段顺序或机械
  问句冒充智能分析。模型必须返回目标、边界、验收标准、置信度、下一问题、
  Worker Provider、角色模板、规则和有向无环任务计划的结构化契约。
- 模型输出不满足契约时，同一 Provider 会话只允许一次有界修复调用；仍不满足、
  Provider 不可用或调用失败时进入 `provider_suspended`。此状态不创建 Goal、
  不派发 Worker，也不降级为机械问卷。
- 人类可在 Provider 恢复后显式续接同一 intake 会话。续接必须携带当前
  `expected_revision`，旧修订不能覆盖新状态。
- Answer 必须匹配当前 `expected_field` 和 `expected_revision`。
- `/messages` 是主对话入口。澄清中由服务端决定该轮回答写入哪个字段；
  用户的质疑、纠正或补充先作为自然语言对话交给模型，不会盲目写入某个字段。
  每次有效修订都会生成新 revision 和 proposal hash，使旧确认自然失效。
- Confirmation 必须来自 `actor_type=human`，并同时匹配 revision 与
  proposal hash。Agent 或全局管家不能替人确认。
- 确认前不创建 Goal/Task、不探测或唤醒 Provider；确认时先验证全部将使用的
  Provider，再以 conversation-scoped 幂等键复用既有 Goal 创建链。
- `default_butler_provider` 决定项目管家的规划模型（默认 Codex CLI）；
  指令中的 `provider`、`role_providers` 与计划项 Provider 决定后续 Worker。
  例如“由 Cursor 执行”不会把项目管家本身切换为 Cursor。
- 四条开发原则由控制面强制加入每个开发角色的规则快照。模型可以增加有效规则，
  但不能删除或裁剪这四条原则。

## 角色模板与实例

规则解析优先级：`direct human Task+role > ProjectRoleRule > RoleTemplate >
global`。系统仅有一个 GlobalButler 与每项目事务内唯一 ProjectButler；其余
开发角色由项目管家按 DAG 动态匹配/生成 RoleTemplate，并创建不可变
RoleInstance + SessionBinding。模型 Provider 在无有效实例或 hash/revision
不匹配时确定性拒绝 dispatch；`simple-worker` / `generic-command` 例外仅限
`model_invoked=false` 且受限本地 CommandSpec。

agency-agents-zh 仅作 MIT 有界结构参考（上游 commit 与源文件 SHA-256 记入
`source_refs`），不 vendoring、不执行 install 脚本。

## 并行拆分

XS 使用 simple-worker；S/M 使用单个 fullstack。L/XL 的默认语义角色来自
体量信号：backend、frontend、ui，以及迁移或部署场景下的 devops_sre。
这些角色默认无依赖并同时进入 `ready`。调用方也可提交有界 `plan_items`，
其中只有明确的 `depends_on_ordinals` 会形成串行边；校验器拒绝未知角色、
向后依赖、缺号和超过策略上限的计划。

共享工作树中的 backend / frontend / devops_sre 里程碑按 DAG 串行，不按
任务标题或 Provider 特判。`role_providers` 可以覆盖某个角色的默认 Provider。
Provider 选择属于工作项，不是项目管家永久会话属性。

## API

- `GET /api/butlers/global/overview`
- `POST /api/butlers/global/route`
- `POST /api/projects/{project_id}/butler/conversations`
- `GET /api/projects/{project_id}/butler/conversations`
- `POST /api/projects/{project_id}/butler/conversations/{id}/messages`
- `POST /api/projects/{project_id}/butler/conversations/{id}/resume`
- `POST /api/projects/{project_id}/butler/conversations/{id}/confirm`
- `GET /api/rules` / `GET /api/role-templates` / `GET /api/role-instances`
- `GET /api/session-bindings` / `GET /api/projects/{id}/role-rules`

旧 `/answers` 端点暂时只作为兼容接口；Web 不再调用。Web 的目标提交只走
上述 conversation/messages/confirm 链。`POST /api/goals` 暂时保留为兼容
控制面原语，`POST /api/tasks` 仍只用于诊断。
