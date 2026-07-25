# LIVE-DS-23/25/26/27/28 Cursor CLI 对照轨回归证据

- Goal: `[E2E_LIVE_DS23_VERIFY_20260725_222004]`
- Task: `c7e70e030c4f499ea7425bd322066ffe` · spec revision 2
- Provider: `cursor_cli`（唯一；未递补 deepseek / codex_cli / kimi）
- 审计日: 2026-07-25
- 范围: 只读核对已落地补丁 + 有界 pytest；本 Goal 不重写实现
- 本交付唯一写入: `docs/runtime-audits/LIVE_DS_23_CURSOR_REGRESSION_EVIDENCE_20260725.md`

## 总判

**有界验收通过（补丁锚点齐全 + 相关单测通过）。**

| 项 | 结论 |
| --- | --- |
| LIVE-DS-23（缺 Artifact + 无进度 → 拒同构继续） | 代码锚点存在；`test_continue_policy` 覆盖 |
| LIVE-DS-25（Planner 结构化结果优先生效，不唯 exit 0） | `lifecycle.py` 锚点存在 |
| LIVE-DS-26（completed + staged/已应用 payload 即落地/succeeded） | `host_bridge.py` / `execution.py` 锚点存在 |
| LIVE-DS-27（acceptance 覆盖子集即可；`recheck_command` 可选） | `verification.py` + `test_checker_result_ingest` |
| LIVE-DS-28（Prompt 允许 mid-line marker；recheck 标注 optional） | Prompt 语义已落地（无字面 `LIVE-DS-28` 注释标签） |
| 指定 pytest | 见下文：系统 `python3 -m pytest` 缺模块；等价解释器下 **6 passed** |

台账侧当前多为「已实现待回归」。本 Task 硬边界仅允许写本证据 md，**未改** `docs/BASELINE_V2_REMEDIATION_LEDGER.zh-CN.md`；若 Owner/Butler 要闭环台账，可据本报告将 LIVE-DS-23/25/26/27/28 升为已闭环或保留待更广回归。

## 主线证据

### 1) 文件与注释锚点（只读）

| ID | 路径 | 锚点（行号约） | 语义摘要 |
| --- | --- | --- | --- |
| LIVE-DS-23 | `plowwhip/continue_policy.py` | L72–84 `reject_isomorphic_empty_delivery_continue` | 缺声明 Artifact / 空 formal manifest + no-progress → 返回拒绝对同构「继续」的 wait_reason |
| LIVE-DS-23 | `plowwhip/lifecycle.py` | L146–163 `_reject_continue_for_empty_delivery`；L1504–1509 调用点 | decision_continue 路径接入拒同构；`fault_code='scope'` |
| LIVE-DS-25 | `plowwhip/lifecycle.py` | L2615–2618 | completed HostJob + 可解析 Planner 结构化结果优先于 nonzero exit |
| LIVE-DS-26 | `plowwhip/host_bridge.py` | L906–918 | `status==completed` 且 staged 有 payload 时即使 `returncode!=0` 仍 apply |
| LIVE-DS-26 | `plowwhip/execution.py` | L1847–1851 | `applied_delta` / `workspace_changed` 使 nonzero exit 仍计 succeeded |
| LIVE-DS-27 | `plowwhip/verification.py` | L1894–1921 `_parse_checker_verdict` | `expected ⊆ reported`；空 `recheck_command` 默认填充，不假拒 PASS |
| LIVE-DS-24/28 解析侧 | `plowwhip/verification.py` | L1876–1881 | 不要求 marker 整行开头；委托 `extract_structured_json` |
| LIVE-DS-28 Prompt | `plowwhip/verification.py` | L1720–1726、L1751–1757 | Prompt 写明 marker *may appear mid-line*；`recheck_command` recommended but optional |
| LIVE-DS-28 Prompt | `plowwhip/lifecycle.py` | L2932–2937 | Planner-checker Prompt 同样 mid-line + optional recheck |

说明：仓库内 **无** 字面注释字符串 `LIVE-DS-28`（仅台账条目）；验收按台账「Prompt 允许 mid-line；recheck optional」语义核对，上述 Prompt 文案已满足。

辅助测试锚点（只读，未改 tests/）：

- `tests/test_continue_policy.py` L1：标注 LIVE-DS-23
- `tests/test_checker_result_ingest.py` L81：标注 LIVE-DS-27（空 recheck + extra acceptance 仍 PASS）

### 2) 命令输出摘要

**指令命令（仓库根）：**

```text
python3 -m pytest tests/test_continue_policy.py tests/test_checker_result_ingest.py -q
```

**实际输出（本机默认 `python3` = Homebrew 3.13）：**

```text
/opt/homebrew/opt/python@3.13/bin/python3.13: No module named pytest
exit: 1
```

**等价有界回归（同仓库根、同两文件；使用已含 pytest 的解释器）：**

```text
…/.venv/bin/python -m pytest tests/test_continue_policy.py tests/test_checker_result_ingest.py -q
......                                                                   [100%]
6 passed in 0.34s
exit: 0
```

结论：指定测试集合在可解析依赖的解释器下全部通过；默认 `python3` 环境缺 `pytest` 属于运行时环境缺口，**不是**补丁逻辑失败。

## 机制验证

1. **LIVE-DS-23（继续策略）**  
   `wait_indicates_missing_delivery` + `recent_no_progress_failure` 同时成立时，`reject_isomorphic_empty_delivery_continue` 返回明确 wait_reason；`lifecycle` 在 verify→continue 分支先于同构 `decision_retry` 调用，避免 MECH-07 式烧 Token 重开同等 HostJob。

2. **LIVE-DS-25（Planner 完成语义）**  
   仅当 HostJob `status != completed` 才在解析前失败；completed 后优先吃结构化 `PLOWWHIP_PLANNER_RESULT`，对齐 Cursor/json-worker 常 exit≠0 但已写结果的行为。

3. **LIVE-DS-26（Execute/Bridge 落地）**  
   Bridge：`apply_workspace_on_success` + completed +（exit0 **或** staged payload）→ apply。  
   Execution：succeeded = completed ∧（exit0 ∨ applied_delta ∨ workspace_changed），避免「已写 Artifact 却因 exit≠0 不 finalize」。

4. **LIVE-DS-27/28 + LIVE-DS-24（Checker 契约）**  
   解析：覆盖全部 frozen `acceptance_id` 即可（允许 extras）；`recheck_command` 可选。  
   Prompt：与解析一致，允许 mid-line marker，避免「整行开头」指示与 Cursor 信封输出冲突导致伪拒。

5. **单测交叉**  
   continue_policy 与 checker_result_ingest 共 6 例通过，覆盖拒同构继续、嵌套/信封 ingest、可选 recheck 等主路径。

## 风险与建议

1. **环境**：默认 `python3 -m pytest` 在本对照轨主机上不可用；CI/验收脚本应固定带 pytest 的解释器或声明 `PYTHONPATH`/venv，否则「命令失败」易被误判为回归失败。
2. **LIVE-DS-28 标签**：实现语义已在 Prompt 落地，但源码无 `LIVE-DS-28` 注释；后续若做静态台账扫描，建议补一行注释或接受「语义验收」口径。
3. **台账状态**：硬边界未改台账；本报告可作为「Cursor 对照轨有界回归」证据。更广 E2E（真实 Cursor HostJob 全链路）仍属台账「待回归」的可选下一跳，非本 Goal 范围。
4. **边界遵守**：本 Task 未改 `plowwhip/`、`tests/`、Dockerfile、密钥；未跑 git/Docker/deploy/外发；唯一 Artifact 为本 md。
)
