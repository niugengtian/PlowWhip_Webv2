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

宿主机只需要 Docker；Python、Node 构建环境、SQLite 运行库、Web 服务和零 Token Cron engine 都在镜像内。

```bash
docker compose up --build -d
open http://127.0.0.1:8742
```

- `plow-whip-web-v2-data`：SQLite、WAL、日志、上下文归档和备份。
- `plow-whip-web-v2-projects`：受管项目工作区，对应容器内 `/projects`。
- Compose 使用 `restart: unless-stopped`，Docker 恢复后自动拉起；休眠或停机错过的计划默认只补跑一次。
- 控制面板 Settings → Crontab 管理支持启停、标准五段表达式、时区、错过执行策略和立即 Tick。
- 只有一个全局计划扫描所有项目/角色/Worker/Task；数据库租约与 fencing token 防止重复调度和脑裂。
- 控制路径只做 SQLite 扫描、网络探测和状态判断，模型调用数与 Token 消费均为 0。

## 本机 CLI Worker 池

Docker 容器不能直接执行 macOS 二进制。Codex CLI、Cursor CLI 和 simple-worker 通过受限 Host Bridge 注册为 Worker Provider；它只接受结构化请求、固定适配器和已声明的项目根，不提供任意 shell 接口。

仅在需要本机 CLI 时启动桥：

```bash
export PLOW_WHIP_BRIDGE_TOKEN="$(openssl rand -hex 24)"
PLOW_WHIP_BRIDGE_TOKEN="$PLOW_WHIP_BRIDGE_TOKEN" docker compose up --build -d
.venv/bin/python -m plow_whip_web.host_bridge \
  --project-root /Users/your-name/work
```

先启动容器，再让 Host Bridge 在当前终端持续运行；两者必须使用同一个令牌。关闭桥只会让本机 CLI Worker 进入不可用状态，不影响控制面板和零 Token 调度继续工作。

项目注册时分别填写容器路径和本机路径。容器 Worker 使用 `/projects/...`；Codex/Cursor/simple-worker 使用 `/Users/...`。控制台 Provider 页可执行 0 Token 探测。平台 API Key 不是启动前提；未来凭据只通过环境变量引用接入，不保存在页面、SQLite、日志或镜像里。

查看状态和日志：

```bash
docker compose ps
docker compose logs -f control-plane
docker compose exec control-plane python -m plow_whip_web --data-dir /data scheduler-tick
```

不要把 SQLite 写进镜像层。数据使用 named volume，因此重建/升级镜像不会丢失；只有明确执行 `docker compose down -v` 才会删除数据卷。

Sprint 0–8 已完成：这是可构建、可运行、可审计、可恢复的容器化 Web MVP。真实 CLI Provider 在 Host Bridge 未配置时会明确阻塞，不会伪装完成；内置 Generic Command 可独立完成全链路验收。
