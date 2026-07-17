# Sprint 10 Task Preflight Semantics Result

Status: COMPLETE

## UI 语义前后对照

| 场景 | 收口前 | 收口后 |
| --- | --- | --- |
| 新建任务 | 用户可选“快速 / 均衡 / 严格”，暗示不存在的质量能力 | 删除质量档位 selector；API client 在创建边界固定发送 `quality_profile=deterministic` |
| 任务详情 | 展示“质量档位”和历史 `fast / balanced / strict / deterministic` 原值 | 展示“验证机制：确定性验证”；所有历史兼容值使用同一确定性验证语义 |
| 独立复审 | “需要独立复审”没有说明当前能力边界 | 保留输入并明确提示：勾选会触发 Planning Gate；当前没有独立 reviewer 编排，任务不能入队 |
| 独立复审预判 | `independent_review_orchestration` 没有中文映射 | 显示“尚无独立 reviewer 编排，要求独立复审的任务当前不能入队” |
| 创建授权 | 依赖最近一次成功 preflight；修改 sizing 或请求失败会失效 | 保留该约束：只有当前 sizing inputs 对应的最新 `estimated` 响应可提交；`needs_planning`、missing gate、输入变更和预判失败均阻止创建 |
| 预算展示 | 展示服务端 sizing 结果，但仍有旧质量档位语义 | 展示服务端 Tier、Token 预算、Soft/Hard Timeout、最大尝试及 Token 区间；不在前端计算预算 |
| 超时默认 | 创建请求按 Provider 在前端写死 60/600 秒 | 创建请求不再写固定命令超时，由服务端契约负责运行预算 |

## 契约证据

- 正常 `estimated` 创建请求携带与 preflight 完全相同的 `sizing_inputs`。
- API client 统一注入 `quality_profile=deterministic`，表单没有可修改该值的状态或控件。
- UI 直接展示 estimate 响应中的 `size_class`、`total_token_hard_cap`、`soft_deadline_seconds`、`hard_deadline_seconds` 和 `max_attempts`；没有复制估算算法，也没有写固定 50k/600s。
- `missing_gates` 类型和中文原因覆盖 `independent_review_orchestration`。
- 组件测试覆盖：无质量档位 selector、创建请求 deterministic、独立复审阻止创建并显示原因、正常创建复用 sizing inputs、输入变更和请求失败使 preflight 失效、四种历史质量值统一展示为确定性验证。

## 修改范围

- `web/src/App.tsx`
- `web/src/api.ts`
- `web/src/App.test.tsx`
- `docs/SPRINT_10_TASK_PREFLIGHT_SEMANTICS_RESULT.md`

未修改 backend、migration、部署文件、依赖或 lockfile；未提交、未推送，且保留了工作树全部既有脏改动。

## 验证证据

- `cd web && pnpm test`：PASS，1 个测试文件、12 项测试通过。
- `cd web && pnpm run typecheck`：PASS。
- `cd web && pnpm run lint`：PASS。
- `cd web && pnpm run build`：PASS，Vite 生产构建完成。
- `git diff --check`：PASS。

SPRINT10_TASK_PREFLIGHT_SEMANTICS_COMPLETE
