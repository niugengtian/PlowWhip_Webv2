# PlowWhip Web V2 本机功能脚本集

统一入口：

```bash
./scripts/plow-whip-web --help
./scripts/plow-whip-web --version
./scripts/plow-whip-web -man
```

也支持短参数 `-h` 和 `-v`。`-man`、`--manual`、`manual` 会输出完整操作手册。
如需在任意目录直接使用命令：

```bash
./scripts/plow-whip-web install-cli
plow-whip-web -h
plow-whip-web -v
```

默认安装到 `~/.local/bin/plow-whip-web`。若该目录尚未加入 `PATH`，安装结果会
输出对应的 `export PATH=...` 提示。只删除这个命令入口：

```bash
plow-whip-web uninstall-cli
```

默认行为是从 `https://github.com/niugengtian/PlowWhip_Webv2.git` 获取最新
`main`，源码放在 `~/.plow-whip-web/releases/<commit-sha>`。它不会切换、重置、
清理当前开发工作区，也不会把 `.env.local` 放进 Docker 构建上下文。

## 首次初始化与配置

```bash
# 创建私有配置、生成 Bridge token、获取 GitHub main、准备 Bridge venv
./scripts/plow-whip-web init

# 设置 Host Bridge 允许访问的项目根；可重复传入多个目录
./scripts/plow-whip-web configure \
  --project-root /Users/you/work \
  --timezone Asia/Shanghai

# 可选：从私有文件导入 DeepSeek Key，避免 Key 出现在命令行参数中
chmod 600 /private/path/deepseek.key
./scripts/plow-whip-web configure --deepseek-key-file /private/path/deepseek.key
```

配置保存在 `~/.plow-whip-web/ops.json`，私密环境保存在
`~/.plow-whip-web/.env.local`，两者权限均为 `600`。Bridge token 不会写入
launchd plist、镜像或 Git。

## 构建、启动和重启

```bash
# 默认：重新获取 GitHub 最新 main，构建镜像，重建 container，
# 并用同一版本代码安装/重启 Host Bridge
./scripts/plow-whip-web rebuild

# 指定分支、tag 或 commit
./scripts/plow-whip-web rebuild --ref release/v1
./scripts/plow-whip-web rebuild --ref 0123456789abcdef0123456789abcdef01234567

# 使用其他仓库或本地代码；本地工作树不会被 reset/clean
./scripts/plow-whip-web rebuild --repo https://github.com/example/fork.git --ref main
./scripts/plow-whip-web rebuild --source local --local-source "$PWD"

# 拉取最新基础镜像并禁用 Docker build cache
./scripts/plow-whip-web rebuild --pull --no-cache

# 单独控制组件
./scripts/plow-whip-web restart container
./scripts/plow-whip-web restart bridge
./scripts/plow-whip-web restart all
./scripts/plow-whip-web start all

# 启动前主动升级到已配置来源的最新版本
./scripts/plow-whip-web start all --latest --pull
```

所有写操作共用 `~/.plow-whip-web/ops.lock`，防止两个 Compose/Bridge writer
并发。检测到活跃 Host Job 时，重建和重启默认拒绝执行；`--force` 只用于操作者
已经确认任务可以中断的情况。

## 停止、状态与卸载

```bash
./scripts/plow-whip-web status
./scripts/plow-whip-web stop bridge
./scripts/plow-whip-web stop container
./scripts/plow-whip-web stop all

# 删除 LaunchAgent 和 container，但默认保留 SQLite/项目 named volumes、镜像和配置
./scripts/plow-whip-web uninstall all

# 显式删除镜像
./scripts/plow-whip-web uninstall all --remove-image

# 危险操作必须同时给出 --yes
./scripts/plow-whip-web uninstall all --purge-data --purge-runtime --remove-image --yes
```

`uninstall` 默认不带 `-v`，因此 `plow-whip-web-v2-data` 和
`plow-whip-web-v2-projects` 会保留。只有 `--purge-data --yes` 才删除命名卷。

## 常用配置参数

```bash
# 永久改为 GitHub 某分支
./scripts/plow-whip-web configure --source github --ref main

# 永久改为本地源码
./scripts/plow-whip-web configure --source local --local-source /path/to/repo

# 轮换 Bridge token；轮换后应 restart all，使 container 和 Bridge 同时读取新 token
./scripts/plow-whip-web configure --rotate-bridge-token
./scripts/plow-whip-web restart all

# 修改 Bridge 端口时，同时修改容器访问 URL
./scripts/plow-whip-web configure \
  --bridge-port 8876 \
  --bridge-url http://host.docker.internal:8876
```

运行态验证包括控制面 `/health`、SQLite WAL、镜像 release SHA、宿主机 Bridge
鉴权探测，以及 `all` 路径下从 container 到 `host.docker.internal` 的 Bridge 探测。
