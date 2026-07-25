# V2 Docker 8750 运行验收 Evidence

> 时刻：2026-07-25  
> 源码 tip：`blue@5c0dcda`（镜像构建自该工作树）  
> 未调用付费 Provider；未切流到其它端口

## 1. 构建与替换

| 项 | 值 |
|---|---|
| 构建命令 | `docker build --network none -t plowwhip-web:v2-baseline -t plowwhip-web:v2-blue-5c0dcda .` |
| 镜像 | `plowwhip-web:v2-baseline`（manifest `sha256:27a444135ba1db29f6d3af0ea319ab21f1b72747ffd9e1dbdecede1b79bcb181`） |
| 旧容器 | `plowwhip-web-v1-8750-r48-retired`（`plowwhip-web:v1-r48`） |
| 新容器 | `plowwhip-web-v1-8750` |
| 端口 | `127.0.0.1:8750 -> 8742/tcp` |
| 数据卷 | `plowwhip-web-v1-8750-data:/data`（保留） |
| 用户 | `65534:65534` |
| 重启策略 | `unless-stopped` |
| Host 映射 | `host.docker.internal:host-gateway` |
| 环境 | 自旧容器继承 Bridge URL/Token/探针路径（未改卷内业务数据） |

健康检查：

```text
health=healthy
GET /health → {"cronner":"enabled","status":"ok"}
```

容器内模块：`lifecycle_state` / `artifact_contract` / `secret_policy` 可 import。

## 2. A-07 / 调度运行事实

| 事实 | 证据 |
|---|---|
| 单容器持有 8750 | `docker ps` 仅该容器 publish 8750 |
| Cronner 应用内 | Monitor `cronner.mode=in_process`，`entry=advance_project` |
| 调度锁文件 | `/data/.cronner.lock` 存在 |
| 数据卷唯一 | 继续使用 `plowwhip-web-v1-8750-data` |
| schema | `schema_version=10`，`tables=15`，`journal_mode=wal`，`quick_check=ok` |
| 当前负载 | projects=8，tasks=24，task_sessions=53，due_actions=0，active_leases=0 |

结论：**A-07 本地单实例调度资格在本机 Docker 上可验收为已实现**（跨错误挂载/多机双活仍未测）。

## 3. API 实测（8750）

| 接口 | 结果 |
|---|---|
| GET `/health` | 200 ok + cronner enabled |
| GET `/api/projects` | 200，8 项目 |
| GET `/api/monitor` | 200，schema 10 / 15 表 |
| GET `/api/token` | 200 |
| GET `/api/settings-library` | 200 |
| GET `/api/search?q=result.txt` | 200 |
| GET `/api/butler?project_id=check-code` | 200 |
| GET `/api/tasks/<done-task>` | 200；含 `observation_notice`、artifacts、sessions、handoffs |
| GET Task 文件（hash URL） | 200；返回 `X-Content-SHA256` |
| POST `/api/actions` wake（已完成 Task） | 400 拒绝（符合终态保护） |
| POST `/api/semantic-search` | 202（第三写入口仍存在，与 B-15 冲突一致） |

Task 快照关键字段：

- `observation_notice` =「仅用于有界观察；不是 Artifact、Evidence 或完成依据。」
- `task.role_key=fullstack`，`checker_role_key=independent_checker`
- artifacts 可按 Task ID + sha256 打开完整文件

## 4. 浏览器实测（opencli → http://127.0.0.1:8750/）

环境：`opencli doctor` 全绿。

| 场景 | 结果 |
|---|---|
| 首页加载 | 标题「Plow Whip · 无人值守控制台」；健康「控制面在线」；指标 8/0/8/0 |
| 四类顶栏 | 全局首页 / 项目详情 / Task 详情 / 设置与资源库 |
| 选中「审查代码」 | 项目管家历史可见；Current Task 显示已完成 |
| Task 详情板 | 12 个 Task 全在「已完成」泳道；Role/Checker/Flow Token 可见 |
| Task Inspector | Role=`fullstack / independent_checker`；Artifact 列表含 sha256 与「打开完整文件」 |
| 观察合同 | 文案「仅用于有界观察；不是 Artifact、Evidence 或完成依据。」 |
| 设置页 | Token 计量 + Monitor 只读 + Provider Diagnostics + 项目阈值表单 |
| 精确搜索 `result.txt` | 返回索引命中（含历史 semantic query 消息） |
| Library | 角色文件显示「SHA 匹配」 |

未做（避免付费）：自然语言正式指令入队、极小 Token 探针、真实 Planner HostJob。

## 5. 与基线差距（运行态复核）

运行态**不推翻**合入后重审的代码合同结论：

| 条款 | 运行态观察 |
|---|---|
| A-07 | **升为已实现（本机 Docker）** — 单容器 + `.cronner.lock` + in-process Cronner |
| B-15 | 仍冲突 — UI/API 可走 `POST /api/semantic-search` |
| L-01/C-02 等 | 运行态无法单独证伪；代码路径仍在 tip 中 |
| D-30 | 未做明文 Secret 注入负向测试 |

## 6. 回滚点

```text
旧容器名：plowwhip-web-v1-8750-r48-retired
旧镜像：plowwhip-web:v1-r48
数据卷未删除：plowwhip-web-v1-8750-data
```
