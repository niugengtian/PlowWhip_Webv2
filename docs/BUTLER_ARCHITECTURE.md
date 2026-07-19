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

## Intake 状态机

```text
instruction
  -> clarifying(objective | boundaries | acceptance，一次一个)
  -> awaiting_confirmation(confidence=95, proposal_hash)
  -> dispatched(goal_id)
```

- 指令本身作为初始 objective；显式 objective 可以覆盖它。
- objective、boundaries、acceptance 的权重分别为 35、30、30。缺少任一项
  都不会生成提案。
- Answer 必须匹配当前 `expected_field` 和 `expected_revision`。
- `/messages` 是主对话入口。澄清中由服务端决定该轮回答写入哪个字段；
  待确认时，人可选择目标、边界或验收标准继续发消息修订。每次修订都会生成
  新 revision 和 proposal hash，使旧确认自然失效。
- Confirmation 必须来自 `actor_type=human`，并同时匹配 revision 与
  proposal hash。Agent 或全局管家不能替人确认。
- 确认前不创建 Goal/Task、不探测或唤醒 Provider；确认时先验证全部将使用的
  Provider，再以 conversation-scoped 幂等键复用既有 Goal 创建链。

## 并行拆分

XS 使用 simple-worker；S/M 使用单个 fullstack。L/XL 的默认语义角色来自
体量信号：backend、frontend、ui，以及迁移或部署场景下的 devops_sre。
这些角色默认无依赖并同时进入 `ready`。调用方也可提交有界 `plan_items`，
其中只有明确的 `depends_on_ordinals` 会形成串行边；校验器拒绝未知角色、
向后依赖、缺号和超过策略上限的计划。

`role_providers` 可以覆盖某个角色的默认 Provider。Provider 选择属于工作项，
不是项目管家永久会话属性。

## API

- `GET /api/butlers/global/overview`
- `POST /api/butlers/global/route`
- `POST /api/projects/{project_id}/butler/conversations`
- `GET /api/projects/{project_id}/butler/conversations`
- `POST /api/projects/{project_id}/butler/conversations/{id}/messages`
- `POST /api/projects/{project_id}/butler/conversations/{id}/confirm`

旧 `/answers` 端点暂时只作为兼容接口；Web 不再调用。Web 的目标提交只走
上述 conversation/messages/confirm 链。`POST /api/goals` 暂时保留为兼容
控制面原语，`POST /api/tasks` 仍只用于诊断。
