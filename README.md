# plow-whip Web v2

在保障质量的前提下实现无人值守完成，并尽量减少 Token 消费。

这是完全独立的新实现，不依赖旧 `plow-whip` 代码、状态文件、Desktop Thread 或厂商 Agent Runtime。

## 目录

- `backend/plow_whip_web/`：FastAPI、领域状态机、SQLite、Runtime 和 Provider Adapter
- `web/`：React + TypeScript + Vite 控制台
- `tests/`：Backend 单元、集成、故障注入和 E2E
- `docs/`：产品、架构、Sprint 和验收证据
- `runtime/`：本机数据库、日志和归档，不进入 Git

## Sprint 0 开发命令

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest
.venv/bin/python -m plow_whip_web --data-dir runtime

cd web
pnpm install
pnpm test
pnpm run typecheck
pnpm run lint
pnpm run build
```

Backend 默认监听 `127.0.0.1:8742`，Frontend 开发服务器默认监听 `127.0.0.1:5173`。

## Docker 一键运行（推荐）

需要统一管理 GitHub 最新 `main`、Docker 镜像/container 和 macOS Host Bridge 时，
可使用本机功能脚本集：

```bash
./scripts/plow-whip-web init
./scripts/plow-whip-web rebuild
./scripts/plow-whip-web status
./scripts/plow-whip-web stop all
./scripts/plow-whip-web install-cli  # 之后可直接运行 plow-whip-web -h / -v / -man
```

它还提供 `configure`、`start`、`restart` 和安全的 `uninstall`。完整参数与数据保留
规则见 [本机功能脚本集](docs/OPS_TOOLKIT.zh-CN.md)。container 启动、重启和
重建前会强制验证现有 SQLite 与目标源码的迁移血统；详细约束见
[迁移血统收敛](docs/MIGRATION_LINEAGE_RECONCILIATION.md)。

宿主机只需要 Docker；Python、Node 构建环境、SQLite 运行库、Web 服务和零 Token Cron engine 都在镜像内。macOS/Linux 使用 Bash，Windows 使用 PowerShell：

```bash
# macOS / Linux
cp .env.local.example .env.local
chmod 600 .env.local
# 编辑 .env.local：填写 PLOW_WHIP_BRIDGE_TOKEN；需要 DeepSeek 时再填写 DEEPSEEK_API_KEY
SHA="$(git rev-parse HEAD)"
python3 scripts/release_local.py deploy --expected-sha "$SHA"
```

```powershell
# Windows PowerShell
Copy-Item .env.local.example .env.local
icacls .env.local /inheritance:r /grant:r "$($env:USERNAME):(R,W)"
# 编辑 .env.local 后启动
$sha = git rev-parse HEAD
py scripts\release_local.py deploy --expected-sha $sha
```

浏览器打开 `http://127.0.0.1:8742`。

- `.env.local` 是本机私密配置，已被 Git 忽略；仓库只提交不含密钥的 `.env.local.example`。不要在聊天、SQLite、Compose 文件或日志中粘贴真实 Key。
- `plow-whip-web-v2-data`：SQLite、WAL、日志、上下文归档和备份。
- `plow-whip-web-v2-projects`：受管项目工作区，对应容器内 `/projects`。
- Compose 使用 `restart: unless-stopped`，Docker 恢复后自动拉起；休眠或停机错过的计划默认只补跑一次。
- 控制面板 Settings → Crontab 管理支持启停、标准五段表达式、时区、错过执行策略和立即 Tick。
- 只有一个全局计划扫描所有项目/角色/Worker/Task；数据库租约与 fencing token 防止重复调度和脑裂。
- `max_parallel_workers` 同时约束跨 Tick 已在途任务和手工派发；Token 只写入消费账本，不参与任务准入、调度、熔断、续跑或终态。
- 逻辑 Worker 绑定 `project + role`，物理 Codex/Cursor Session 绑定 `project + role + Task`；同 Task 重试可续接，换 Task 不继承旧聊天或工具历史。
- Settings 将 Context、checkpoint、handoff、观察尾部、文件轮转、同类失败和无进展阈值放在一起；Task+角色特例 > Project > Global，提交时显示来源并校验冲突。
- 控制路径只做 SQLite 扫描、网络探测和状态判断，模型调用数与 Token 消费均为 0。
- **两级管家、项目隔离**：全局管家只读汇总已注册项目的 Goal/Task/Worker 规范状态，并把指令路由到项目管家；它不共享项目会话，也不摄取全量文件或聊天。项目管家是逻辑常驻入口，缺少目标、边界或验收标准时一次只问一个问题，服务端达到 95% 把握后生成带哈希方案，只有人类确认才会执行。
- **大型目标按真实依赖并行**：XS 路由到 simple-worker，S/M 路由到单个 ephemeral fullstack；L/XL 默认拆成 backend/frontend/ui/devops_sre 等语义角色，独立工作项同时进入 `ready`，显式依赖才形成串行边。每个 Task 自带 verification Gate，物理 Provider Session 仍严格绑定 `project + role + Task`，临时 Worker 在证据终态后释放。

## 本机 CLI Worker Pool

Docker 容器不能直接复用宿主机 CLI、认证和项目目录。Codex CLI、Cursor CLI 和 simple-worker 通过受限 Host Bridge 注册为 Worker Provider；它只接受结构化请求、固定适配器和已声明的项目根，不提供任意 shell 接口。

macOS：

```bash
.venv/bin/python scripts/release_local.py install-bridge-macos \
  --project-root /Users/you/work
```

该命令安装用户级 `com.plow-whip-web.host-bridge` LaunchAgent，显式保存可找到
Codex 与 simple-worker 的 `PATH`，登录后自动启动，并拒绝在仍有活跃 Host Job
时替换 Bridge。plist 只保存 `.env.local` 路径，不保存 Token 或 API Key。

Linux：

```bash
.venv/bin/python -m plow_whip_web.host_bridge \
  --env-file .env.local \
  --project-root /home/you/work \
  --state-dir "$HOME/.plow-whip-web/host-bridge"
```

Windows PowerShell：

```powershell
.\.venv\Scripts\python.exe -m plow_whip_web.host_bridge `
  --env-file .env.local `
  --project-root C:\Users\you\work `
  --state-dir "$HOME\.plow-whip-web\host-bridge"
```

`.env.local` 中的 `PLOW_WHIP_BRIDGE_TOKEN` 可用 `openssl rand -hex 24` 生成；`DEEPSEEK_API_KEY` 只由本机 Host Bridge 读取并传给 simple-worker，不传入控制面容器。先启动容器，再让 Host Bridge 持续运行；两者必须读取同一个本地文件。Provider 页面提供三种系统的命令提示和 0 Token 探测。创建任务及每次派发前，后端都会执行真实就绪探测；未通过时任务不会入队或被领取。Host Bridge 会把不含 Prompt 和 argv 的 Host Job 状态写入 `--state-dir`。Bridge 暂时不可达时，容器保留已运行任务的租约并进入 `recovery_hold`，不会把仍可能存活的 CLI 进程重复派发；Bridge 恢复后由零 Token 调度自动对账。

项目注册时分别填写控制面挂载路径和本机项目目录。所有 Web UI 可选 Worker 都通过 Host Bridge 在本机项目目录执行并验收；报告、代码和其他任务产物始终留在原项目目录，Docker 只保存控制面状态和任务声明的产物路径索引。任务详情页通过 Host Bridge 实时确认文件大小、SHA-256 与修改时间，并可复制主机路径、在 Finder 定位或交给 Cursor 打开，不会把文件内容复制进容器。控制台 Provider 页可执行 0 Token 探测。平台 API Key 不是启动前提；凭据只通过环境变量引用接入，不保存在页面、SQLite、日志或镜像里。simple-worker 在缺少 `DEEPSEEK_API_KEY` 时会明确显示不可用，不会伪装就绪。

查看状态和日志：

```bash
docker compose ps
docker compose logs -f control-plane
docker compose exec control-plane python -m plow_whip_web --data-dir /data scheduler-tick
```

发布或升级必须只通过 `scripts/release_local.py deploy`。它用本机文件锁保证同一时刻
只有一个 Compose 写事务，并要求工作树干净、HEAD 与 GitHub 分支精确一致；镜像记录
同一发布 SHA。监控和状态检查只能使用只读 API、`docker ps/inspect/logs` 或
`scripts/release_local.py verify`，不得在观察循环中调用 Compose。

不要把 SQLite 写进镜像层。数据使用 named volume，因此重建/升级镜像不会丢失；只有明确执行 `docker compose down -v` 才会删除数据卷。

Sprint 0–9 已完成：这是可构建、可运行、可审计、可恢复的容器化 Web MVP。真实 CLI Provider 在 Host Bridge 未配置时会明确阻塞，不会伪装完成；内置 Generic Command 只保留为后端确定性测试适配器，不作为 Web UI 项目 Worker 暴露。
