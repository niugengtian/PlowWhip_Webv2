# 数据库迁移血统收敛

## 正式血统

自 2026-07-19 起，正式 SQLite 迁移血统为：

```text
0001_initial.sql
...
0020_provider_context_pressure.sql
0021_remove_token_budget.sql
0022_task_spec_continuity.sql
0023_butler_execution_policy.sql
0024_goal_specs_evidence_manifests.sql
0025_execution_episodes.sql
0026_dispatch_model_calls_worker_stream.sql
0027_task_sessions_bounded_continuity.sql
0028_butler_intake.sql
```

这条血统来自已部署并经过运行验证的 `62acdf36`，再向前应用 task-scoped
session 连续性迁移 `0027`，再追加两级管家会话与确认门迁移 `0028`。

## 已废止的分叉

原 GitHub `main` 曾从共同提交 `d9b84383` 独立发展，并重复使用以下编号：

```text
0021_unified_domain_reducer.sql
0022_butler_intake_help.sql
```

它们与正式血统的 `0021/0022` 不是同一迁移，不能应用到已经运行正式血统的
数据库上。对生产数据库副本执行该分叉的迁移器会在
`0021_unified_domain_reducer.sql` 报告 `table model_calls already exists`。

收敛合并保留了原 `main` 的 Git 历史，但代码树明确选择已经部署、可解释现有
SQLite 的生产血统。不得重新引入上述两个已废止迁移文件，也不得通过手工修改
`schema_migrations` 伪造兼容。

## 发布门禁

`plow-whip-web` 在 container 启动、重启和重建之前执行迁移契约预检：

1. 读取目标源码的迁移文件名和 SHA-256；
2. 从运行 container 或保留的 data volume 读取已应用迁移和 checksum；
3. 要求已应用记录是目标血统的有序前缀；
4. 拒绝未知迁移、checksum 缺失或漂移、目标编号重复；
5. 只允许执行目标血统末尾尚未应用的向前迁移。

该门禁不能通过 `--force` 绕过。

## 验证与恢复

发布前必须使用 SQLite online backup 创建一致性副本，并在副本上验证：

- 第一次迁移只应用预期文件；
- 第二次迁移为空；
- `PRAGMA integrity_check` 返回 `ok`；
- `PRAGMA foreign_key_check` 无结果；
- 核心表行数和业务读取保持一致；
- 候选镜像可通过 `/health`、核心 API 和静态页面检查。

生产部署前应保留旧镜像回滚标签、SQLite online backup 以及 data/projects
volume 归档。不得使用 `docker compose down -v`。
