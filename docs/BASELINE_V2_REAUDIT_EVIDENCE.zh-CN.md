# PlowWhip Web V2 基线复核 Artifact / Evidence

## 1. 复核状态

这是按 168 条唯一 V2 基线逐条重审的当前证据。生产代码、针对性测试、完整回归和 macOS/Linux Host Bridge 合同已经闭合；`A-07` 仍等待获准写入本机 Docker BuildKit 元数据后，才能重建并替换 8750 容器。

当前机械统计：

- 已实现：167
- 部分实现：1（`A-07`，代码与单卷租约存在，8750 新镜像运行验收受桌面沙箱权限限制）
- 仅文档声明：0
- 未实现：0
- 与基线冲突：0

因此本文件当前不是 Goal 完成声明；Docker 验收后必须重新更新 `A-07`、统计和运行证据。

## 2. 权威输入与修复版本

- 唯一基线：`/Users/niugengtian/work/plow-whip-web-v2/docs/MINIMAL_REDESIGN_BASELINE_V2.zh-CN.md`
- 基线 SHA-256：`4ae79ba906997640304a202ba0d453e2437e77e36468c29eb4a185ad970f6098`
- 初始符合性审计：`docs/BASELINE_V2_CONFORMANCE_AUDIT.zh-CN.md`
- 审计 SHA-256：`9b553d741728dc1c5e843148ac28e76efc4a461ac6d74a05d060e77038c72cd8`
- 修复起点：`main@942f246d7b82dcfdcb74452c654646185a27067f`
- 起点 tree：`4b403873193070c374703129a75b28bbd0e53c26`
- 合并根因和生产证据：`docs/BASELINE_V2_REMEDIATION_LEDGER.zh-CN.md`
- SQLite：additive-compatible 升级至 schema version 10，没有删除或重建历史业务表。

## 3. 证据索引

`RC-*` 指向修复台账中的闭合根因。`KEEP-*` 不是沿用旧报告结论，而是重新检查相应生产路径后，确认修复没有破坏原已实现合同：

| 索引 | 当前生产证据 |
|---|---|
| KEEP-P | `planner.py` 的唯一三档语义分类、A/B 合同和 Planner 只提交事实；`lifecycle.py` 安装版本化 Plan。 |
| KEEP-L | `lifecycle.py::advance_project` 单动作推进；`lifecycle_state.py` 与边界测试限制生命周期写入；Monitor 只读。 |
| KEEP-O | `store.py` 仍只有 Project/Goal/Plan/Task/Worker/TaskSession/SessionGeneration/HostJob 主对象；没有重复运行对象。 |
| KEEP-S | `tasks.public_status` 仍是四态，cancelled 只作 outcome；phase/fault 保持内部事实。 |
| KEEP-A | 应用内单 Cronner、due/lease/fence/reconcile 主线、无补跑历史、无日志/心跳完成判定。 |
| KEEP-R | TaskSpec、Artifact、Evidence 与同 Task Checker 主线；没有 Verification Task 或第二结果状态机。 |
| KEEP-M | Hot/Warm/Cold 三层边界、Provider 原生 session 不被修改、Token 缓存子集纪律和 Task 总预算。 |
| KEEP-T | 全局→项目→Task+Role 超时覆盖、TaskSession 冻结和无输出不等于 timeout。 |
| KEEP-D | SQLite WAL 唯一状态库/队列、Library 文件真源、浏览器 ID 边界、无第二 current 状态文件。 |
| KEEP-B | 单进程模块边界、Provider 静态顺序、统一 Butler、有限写 API 和显式外部授权。 |
| KEEP-C | 应用模块直接函数调用；只有 Host Bridge 是宿主 HTTP 边界。 |

## 4. 逐条 168 项复核

| 条款 | 初始审计 | 当前结论 | 证据索引 |
|---|---|---|---|
| P-01 | 与基线冲突 | 已实现 | RC-P0-01 |
| P-02 | 与基线冲突 | 已实现 | RC-P0-01 |
| P-03 | 已实现 | 已实现 | KEEP-P |
| P-04 | 部分实现 | 已实现 | RC-P0-01 |
| P-05 | 与基线冲突 | 已实现 | RC-P0-01 |
| P-06 | 与基线冲突 | 已实现 | RC-P0-01 |
| P-07 | 已实现 | 已实现 | KEEP-P |
| P-08 | 已实现 | 已实现 | KEEP-P |
| P-09 | 与基线冲突 | 已实现 | RC-P0-02 |
| P-10 | 部分实现 | 已实现 | RC-P0-02 |
| P-11 | 部分实现 | 已实现 | RC-P0-02 |
| P-12 | 未实现 | 已实现 | RC-P0-02 |
| P-13 | 已实现 | 已实现 | KEEP-P |
| P-14 | 部分实现 | 已实现 | RC-P0-01 |
| P-15 | 部分实现 | 已实现 | RC-P0-01 |
| L-01 | 与基线冲突 | 已实现 | RC-P0-03 |
| L-02 | 已实现 | 已实现 | KEEP-L |
| L-03 | 与基线冲突 | 已实现 | RC-P0-03 |
| L-04 | 已实现 | 已实现 | KEEP-L |
| L-05 | 已实现 | 已实现 | KEEP-L |
| L-06 | 已实现 | 已实现 | KEEP-L |
| O-01 | 已实现 | 已实现 | KEEP-O |
| O-02 | 已实现 | 已实现 | KEEP-O |
| O-03 | 已实现 | 已实现 | KEEP-O |
| O-04 | 已实现 | 已实现 | KEEP-O |
| O-05 | 部分实现 | 已实现 | RC-P0-02 |
| O-06 | 已实现 | 已实现 | KEEP-O |
| O-07 | 已实现 | 已实现 | KEEP-O |
| O-08 | 已实现 | 已实现 | KEEP-O |
| O-09 | 已实现 | 已实现 | KEEP-O |
| O-10 | 已实现 | 已实现 | KEEP-O |
| O-11 | 已实现 | 已实现 | KEEP-O |
| O-12 | 已实现 | 已实现 | KEEP-O |
| O-13 | 已实现 | 已实现 | KEEP-O |
| O-14 | 已实现 | 已实现 | KEEP-O |
| O-15 | 已实现 | 已实现 | KEEP-O |
| O-16 | 已实现 | 已实现 | KEEP-O |
| O-17 | 已实现 | 已实现 | KEEP-O |
| S-01 | 已实现 | 已实现 | KEEP-S |
| S-02 | 已实现 | 已实现 | KEEP-S |
| S-03 | 已实现 | 已实现 | KEEP-S |
| S-04 | 已实现 | 已实现 | KEEP-S |
| S-05 | 已实现 | 已实现 | KEEP-S |
| S-06 | 已实现 | 已实现 | KEEP-S |
| A-01 | 已实现 | 已实现 | KEEP-A |
| A-02 | 已实现 | 已实现 | KEEP-A |
| A-03 | 部分实现 | 已实现 | RC-P0-02 |
| A-04 | 已实现 | 已实现 | KEEP-A |
| A-05 | 已实现 | 已实现 | KEEP-A |
| A-06 | 已实现 | 已实现 | KEEP-A |
| A-07 | 部分实现 | 部分实现（环境受限） | RC-P3-03 |
| A-08 | 已实现 | 已实现 | KEEP-A |
| A-09 | 已实现 | 已实现 | KEEP-A |
| A-10 | 已实现 | 已实现 | KEEP-A |
| A-11 | 已实现 | 已实现 | KEEP-A |
| A-12 | 已实现 | 已实现 | KEEP-A |
| A-13 | 已实现 | 已实现 | KEEP-A |
| A-14 | 部分实现 | 已实现 | RC-P1-02 |
| A-15 | 已实现 | 已实现 | KEEP-A |
| A-16 | 与基线冲突 | 已实现 | RC-P0-05 |
| A-17 | 与基线冲突 | 已实现 | RC-P0-05 |
| A-18 | 与基线冲突 | 已实现 | RC-P0-05 |
| A-19 | 已实现 | 已实现 | KEEP-A |
| A-20 | 已实现 | 已实现 | KEEP-A |
| A-21 | 已实现 | 已实现 | KEEP-A |
| A-22 | 已实现 | 已实现 | KEEP-A |
| A-23 | 与基线冲突 | 已实现 | RC-P0-04 |
| A-24 | 已实现 | 已实现 | KEEP-A |
| A-25 | 已实现 | 已实现 | KEEP-A |
| R-01 | 部分实现 | 已实现 | RC-P0-04 |
| R-02 | 与基线冲突 | 已实现 | RC-P0-04 |
| R-03 | 与基线冲突 | 已实现 | RC-P0-04 |
| R-04 | 与基线冲突 | 已实现 | RC-P0-04 |
| R-05 | 部分实现 | 已实现 | RC-P0-04 |
| R-06 | 与基线冲突 | 已实现 | RC-P0-04 |
| R-07 | 与基线冲突 | 已实现 | RC-P0-04 |
| R-08 | 部分实现 | 已实现 | RC-P0-05 |
| R-09 | 已实现 | 已实现 | KEEP-R |
| R-10 | 已实现 | 已实现 | KEEP-R |
| R-11 | 已实现 | 已实现 | KEEP-R |
| R-12 | 与基线冲突 | 已实现 | RC-P0-04 |
| R-13 | 已实现 | 已实现 | RC-P0-05 |
| R-14 | 未实现 | 已实现 | RC-P0-04 |
| R-15 | 与基线冲突 | 已实现 | RC-P0-04 |
| R-16 | 部分实现 | 已实现 | RC-P0-04 |
| R-17 | 已实现 | 已实现 | KEEP-R |
| M-01 | 已实现 | 已实现 | KEEP-M |
| M-02 | 已实现 | 已实现 | KEEP-M |
| M-03 | 与基线冲突 | 已实现 | RC-P1-02 |
| M-04 | 已实现 | 已实现 | KEEP-M |
| M-05 | 已实现 | 已实现 | KEEP-M |
| M-06 | 与基线冲突 | 已实现 | RC-P1-02 |
| M-07 | 已实现 | 已实现 | KEEP-M |
| M-08 | 已实现 | 已实现 | KEEP-M |
| M-09 | 部分实现 | 已实现 | RC-P1-02 |
| M-10 | 已实现 | 已实现 | KEEP-M |
| M-11 | 部分实现 | 已实现 | RC-P1-03 |
| M-12 | 与基线冲突 | 已实现 | RC-P1-03 |
| M-13 | 已实现 | 已实现 | KEEP-M |
| M-14 | 已实现 | 已实现 | KEEP-M |
| M-15 | 已实现 | 已实现 | KEEP-M |
| M-16 | 已实现 | 已实现 | KEEP-M |
| M-17 | 已实现 | 已实现 | KEEP-M |
| T-01 | 已实现 | 已实现 | KEEP-T |
| T-02 | 与基线冲突 | 已实现 | RC-P0-02 |
| T-03 | 已实现 | 已实现 | KEEP-T |
| T-04 | 已实现 | 已实现 | KEEP-T |
| T-05 | 已实现 | 已实现 | KEEP-T |
| T-06 | 与基线冲突 | 已实现 | RC-P1-01 |
| T-07 | 与基线冲突 | 已实现 | RC-P1-01 |
| T-08 | 已实现 | 已实现 | KEEP-T |
| T-09 | 与基线冲突 | 已实现 | RC-P1-01 |
| T-10 | 已实现 | 已实现 | KEEP-T |
| D-01 | 已实现 | 已实现 | KEEP-D |
| D-02 | 已实现 | 已实现 | KEEP-D |
| D-03 | 已实现 | 已实现 | KEEP-D |
| D-04 | 已实现 | 已实现 | KEEP-D |
| D-05 | 已实现 | 已实现 | KEEP-D |
| D-06 | 部分实现 | 已实现 | RC-P0-04 |
| D-07 | 已实现 | 已实现 | KEEP-D |
| D-08 | 已实现 | 已实现 | KEEP-D |
| D-09 | 与基线冲突 | 已实现 | RC-P0-03 |
| D-10 | 已实现 | 已实现 | KEEP-D |
| D-11 | 与基线冲突 | 已实现 | RC-P0-04 |
| D-12 | 已实现 | 已实现 | KEEP-D |
| D-13 | 与基线冲突 | 已实现 | RC-P0-04 |
| D-14 | 已实现 | 已实现 | KEEP-D |
| D-15 | 已实现 | 已实现 | KEEP-D |
| D-16 | 已实现 | 已实现 | KEEP-D |
| D-17 | 已实现 | 已实现 | KEEP-D |
| D-18 | 已实现 | 已实现 | KEEP-D |
| D-19 | 已实现 | 已实现 | KEEP-D |
| D-20 | 部分实现 | 已实现 | RC-P1-05 |
| D-21 | 与基线冲突 | 已实现 | RC-P1-06 |
| D-22 | 与基线冲突 | 已实现 | RC-P1-06 |
| D-23 | 已实现 | 已实现 | KEEP-D |
| D-24 | 未实现 | 已实现 | RC-P2-03 |
| D-25 | 部分实现 | 已实现 | RC-P2-03 |
| D-26 | 已实现 | 已实现 | KEEP-D |
| D-27 | 已实现 | 已实现 | KEEP-D |
| D-28 | 与基线冲突 | 已实现 | RC-P2-02 |
| D-29 | 已实现 | 已实现 | KEEP-D |
| D-30 | 与基线冲突 | 已实现 | RC-P1-04 |
| B-01 | 已实现 | 已实现 | KEEP-B |
| B-02 | 部分实现 | 已实现 | RC-P3-01 |
| B-03 | 已实现 | 已实现 | KEEP-B |
| B-04 | 部分实现 | 已实现 | RC-P0-03 |
| B-05 | 与基线冲突 | 已实现 | RC-P0-03 |
| B-06 | 已实现 | 已实现 | KEEP-B |
| B-07 | 已实现 | 已实现 | KEEP-B |
| B-08 | 已实现 | 已实现 | KEEP-B |
| B-09 | 已实现 | 已实现 | KEEP-B |
| B-10 | 已实现 | 已实现 | KEEP-B |
| B-11 | 已实现 | 已实现 | KEEP-B |
| B-12 | 部分实现 | 已实现 | RC-P2-04 |
| B-13 | 部分实现 | 已实现 | RC-P2-01 |
| B-14 | 部分实现 | 已实现 | RC-P2-01 |
| B-15 | 已实现 | 已实现 | KEEP-B |
| B-16 | 已实现 | 已实现 | KEEP-B |
| B-17 | 部分实现 | 已实现 | RC-P1-05 |
| B-18 | 部分实现 | 已实现 | RC-P1-05 |
| B-19 | 部分实现 | 已实现 | RC-P1-05 |
| B-20 | 已实现 | 已实现 | KEEP-B |
| B-21 | 与基线冲突 | 已实现 | RC-P0-06 |
| C-01 | 与基线冲突 | 已实现 | RC-P0-01 |
| C-02 | 与基线冲突 | 已实现 | RC-P0-03 |
| C-03 | 已实现 | 已实现 | KEEP-C |
| C-04 | 部分实现 | 已实现 | RC-P0-03 |

## 5. 已运行验证

- P0/P1/P2 的逐根因命令、反例和相关回归完整记录在修复台账第 4 节。
- P-14/P-15 针对性验证：跨两个项目只允许一个 `waiting=true` Butler question，当前问题解决后下一项才呈现；2 项测试通过。
- macOS Host Bridge：12 项，11 通过、1 项因桌面沙箱缺少 process start identity 跳过。
- Linux Host Bridge：`--network none`、源码只读挂载，12/12 通过。
- 当前完整回归：`/opt/homebrew/bin/python3.13 -m unittest discover -s tests`，`Ran 90 tests ... OK (skipped=1)`。
- `git diff --check`：最终复跑通过。
- 复核表机械计数：168 行，其中 167 `已实现`、1 `部分实现（环境受限）`。
- 禁止对象/配置静态搜索：生产代码中没有 Attempt、ExecutionEpisode、Candidate、`candidate_ready`、`zero_progress`、`DEEPSEEK_MODEL`、`KIMI_MODEL` 或 `worker_template_promoted`；模型环境变量名称仅出现在拒绝/不透传测试中。

## 6. 工作区结果清单

以下 SHA-256 绑定当前生产代码和测试结果；逻辑 revision 为“起点 commit `942f246...` 上的未提交 V2 修复工作区”，SQLite 合同 revision 为 schema version 10。

| 文件 | SHA-256 |
|---|---|
| `plowwhip/app.py` | `ed35030888bdfcc9056aebb7d0d14850d9f77e2f4bdcc7a711361dd03c6a3590` |
| `plowwhip/artifact_contract.py` | `d657f45fbd239d7db75eb952b03843e4bcbe873bbad933db7db98de157349fe3` |
| `plowwhip/butler.py` | `5682caf87f575d0922a1b958aed03db42c6094d2b52a962a25a9e691743e800d` |
| `plowwhip/continuity.py` | `7194fc38d4c956b2d3af7356cff800e6a2ca2d5431853006332c7935115d6ac9` |
| `plowwhip/cronner.py` | `cd3504d26a79d869d5d9a2c14b616954cccbb516ab87d0e145836af3ae42e119` |
| `plowwhip/execution.py` | `2ccb897c465371d58b81a836f5cec8c94705c688adacc0f82a7340f9bd036b80` |
| `plowwhip/host_bridge.py` | `26ee679508caca285737b2e80df997f2d30b46509e60f35cc8a45ed0b33ad271` |
| `plowwhip/intake.py` | `4f4c6a79d8cc01f74864f4ee79f8f735d1f6448547e3e34d4a5f2b799eecbc6d` |
| `plowwhip/lifecycle.py` | `29e0366bbefe8a3d59115b0d036232a92eddbaaf6794d4f1767f045c12119697` |
| `plowwhip/lifecycle_state.py` | `0f49643ee7627d77464ec1b332dea5ca0c22e4ab89c76ccbd5160e1df9ba2d3a` |
| `plowwhip/monitor.py` | `e31555a0a0118b6a0dab53d52f080dfb8aa7949c34a29d357b0c606466b293ba` |
| `plowwhip/planner.py` | `11b5d0c708b50ba6f857b08920feabeca33cae8dafd4f46ae2d9763ddd9caefe` |
| `plowwhip/provider.py` | `9b6c34ab38e15dcc41c4f1a6c05061922c7f39eff740583c89db2c573756141d` |
| `plowwhip/secret_policy.py` | `15013d3573ad5e7dd5a7df187320309de80b17ad406cf9a84601dc1e259bfd56` |
| `plowwhip/store.py` | `08db67ddc1063810a28f77056f43df6f1959bd1ebb92f9cbbab8af354ca6cf63` |
| `plowwhip/stream_capture.py` | `fd734ca7ce89442fdd064475b569ddcd3677866594491b0de3523da3552b4734` |
| `plowwhip/ui.py` | `e7be47f0804de646f072b64d6c4abee7959295b7cbe8eebd4c247b9dd6505ae2` |
| `plowwhip/verification.py` | `7ce6d47dae6116aa75700a59d5338385d2d26d0694e208b64fb8626db2b292ca` |
| `tests/test_artifact_contract.py` | `f5bea2a195ae5f70c92af1ff16f00e692b6b1974fe842147b98c5b5349adfd77` |
| `tests/test_host_bridge.py` | `d26dbfbd7b4e6091398710644c525ed0903c9fb95ddb1fb6fb72cb451f3e9018` |
| `tests/test_lifecycle_ownership.py` | `93940c9dc00afb8980a27b0782243a53b5b5cbb473220a58ecc60b82f373cb2a` |
| `tests/test_review_fixes.py` | `03aa280356c58f9a98da4cef726c47214f5162749a3b872a27d3209d50c0db7f` |
| `tests/test_vertical_slice.py` | `b6bdb21486c300906848b7592d390909927a62162e350fde409fbb23ea9ea16d` |

本 Evidence 文件的最终 SHA-256 必须在 Docker 证据补齐且文件不再变化后从外部计算，避免自引用哈希。

## 7. 当前唯一阻塞

读取确认当前服务为：

```text
container: plowwhip-web-v1-8750
image: plowwhip-web:v1-r48
status: healthy
port: 127.0.0.1:8750 -> 8742/tcp
data volume: plowwhip-web-v1-8750-data -> /data
restart policy: unless-stopped
user: 65534:65534
```

计划使用 `docker build --network none` 从本工作区构建 `plowwhip-web:v2-baseline`，保留旧容器作可恢复回滚点，再以原端口、原命名卷和原重启策略启动新容器。第一次构建在写 `~/.docker/buildx/activity` 时被桌面沙箱拒绝；自动权限审批又因 Codex 用量限制未能发起。未绕过该权限，也未停止、重命名或修改现有健康容器。

Docker 获准后还必须记录：

1. 新镜像 ID/digest 与 revision label。
2. 旧容器可恢复名称、新容器名称和相同数据卷。
3. health、8750 loopback、schema version 10、四类页面/API、单 Cronner lease。
4. 最新 20 行日志；不得读取全量日志。
5. 整个验证过程的 ModelCall 数不增长。
