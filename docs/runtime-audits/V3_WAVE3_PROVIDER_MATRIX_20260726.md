# V3 Wave3 Provider×条款矩阵证据（2026-07-26）

> 范围：控制面共病 + adapter 能力矩阵静态/单测证明  
> 约束：**不调用付费 Provider**；现场 cursor/deepseek/kimi Planner→Done 仍需主人环境另跑  
> 代码基线：`blue` Wave1=`1393c35` · Wave2=`2b23060`

## 1. 矩阵结论

| Provider / 层 | Adapter | Wave3 状态 | 焦点证据 |
|---|---|---|---|
| 控制面 | — | **已测** | A-16a / ND 无 isomorphic continue / failure-signature / T-14 |
| cursor_cli | cursor | **合同已测**；现场 E2E 待跑 | 默认序 #1；flag 传模；脏行 ingest 既有测试 |
| deepseek | json-worker | **合同已测**；现场 E2E 待跑 | 默认序 #2；env 传模；禁 `--model` |
| kimi | json-worker | **同构合同已测**；现场抽检待跑 | 与 deepseek 同 adapter/transport，勿同化 executable |
| codex_cli | codex | **注册合同已测**；非默认序 | flag 传模；不进 B-04' 前两位 |
| local_script | local-script | **门禁已测** | `local_script_runner` 唯一；Wave2 `script_library_search` |

## 2. 自动化命令

```bash
python3 -m unittest tests.test_v3_provider_matrix -q
```

预期：全部 OK。本文件冻结时该套件覆盖控制面共病与默认序/能力矩阵。

## 3. 明确未宣称闭环的项

1. 真实 `cursor_cli` / `deepseek` / `kimi` 无人值守 Planner→Done（需密钥与 Bridge PATH）。
2. `execution`/`verification` 内残余 `write_task_fields` 全量收口（Wave2 仅闭合 checkpoint 第二推进器）。
3. 全量 `tests.test_vertical_slice` 基线可能仍有历史断言漂移，不以单次绿作为全厂闭环。

## 4. 台账纪律

禁止把「DeepSeek 单次 Done」或「Cursor 单次 Done」写成全 Provider 闭环。  
按 **Provider × 条款** 分栏重标；控制面共病可通过矩阵测试升「已实现待回归」，现场 E2E 另开证据文件。
