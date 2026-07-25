# 有界只读代码审查报告

## 总判

经对 plowwhip 项目核心模块（progress_policy.py、result_ingest.py、planner_errors.py、planner.py、lifecycle.py、execution.py、verification.py、provider.py、host_bridge.py）进行只读审查，代码整体架构清晰，模块职责分离合理，异常处理链路完整，验证层对 LLM 输出具有多重防御性检查。**主线风险等级：中等（Medium）**。主要风险集中在无人值守场景下的循环消费、验证回退链的隐蔽降级、以及省 Token 压缩策略可能导致的语义丢失。浅层引用 token 计数与 zero-cost 模式之间存在偏移风险。MECH 机制设计完备但部分边界条件缺乏护栏。

---

## 主线证据

### 模块职责摘要

| 模块 | 职责 | 关键发现 |
|---|---|---|
| `planner.py` | LLM 驱动的任务规划、重试、分类 | 深度序列化 `context.build()` 无大小上限；仅靠 `context_max_bytes` 配置字段，无硬截断 |
| `progress_policy.py` | 决策是否继续、降级、恢复 | 对 `RETRY_PLANNER` 前缀的解析容忍度过宽 |
| `result_ingest.py` | 将 LLM 输出转换为结构化结果 | `__str__` 以 256 字符截断，但无结构化校验 |
| `planner_errors.py` | 规划级异常定义 | 异常体系完整，与 lifecycle 联动好 |
| `lifecycle.py` | 任务会话生命周期 + 持久化 | action routing 依赖字符串集合，无抽象接口 |
| `execution.py` | 执行器/验证器编排、warm handoff 装配 | warm handoff 截断逻辑存在与 mandatory bytes 的竞争条件 |
| `verification.py` | 多模式（strict/permissive/zero）验证 | zero-cost 校验仅检查 `model_invoked=False`，不验证输出语义 |
| `provider.py` | 厂商 provider 适配 + bridge 调用 | 无重试逻辑；bridge 调用失败直接传播异常 |
| `host_bridge.py` | workspace 快照、bridge POST | `_bridge_post` 无超时参数硬编码；SSL 验证由底层决定 |

### 数据流主线

1. 用户 Goal → `planner.py` 调用 provider → 返回方案 → `execution.py` 分配执行/验证角色
2. 执行器输出 → `result_ingest.py` 序列化 → `verification.py` 按模式校验
3. 校验失败 → `progress_policy.py` 决定 retry / escalate / reject
4. 重试耗尽 → `planner_errors.py` 抛出规划异常 → `lifecycle.py` 标记 outcome

问题段落在验证回退链中：`verification.py` 的 `permissive` 模式会跳过部分严格检查；`zero` 模式只检查 token 为零，不验证输出内容完整性。

---

## MECH-01～07

### MECH-01：规划层自动重试与降级（planner.py / progress_policy.py）

**机制**：`planner.py` 支持 `max_retries`、`recovery_mode`；`progress_policy.py` 提供 `should_retry_planner()` 和降级动作。  
**证据**：`planner.py` 第 180-220 行存在重试循环；`progress_policy.py` 的 `CONTINUE_DECISIONS` 包含 `RETRY_PLANNER`、`ESCALATE`、`RECOVER`。  
**风险**：`_reject_continue_for_recovery_cap` 的截断逻辑在 warm handoff 超过 `context_max_bytes` 时仅发 warning，不阻断——在无人值守下可能导致上下文溢出。

### MECH-02：验证多模式切换（verification.py）

**机制**：三种模式 `strict` / `permissive` / `zero`，对应不同严格程度的断言集合。  
**证据**：`verification.py` 第 140-200 行。`strict` 校验 `model_invoked`、input/output token 边界；`zero` 仅校验 `model_invoked=False` 且 token 为零。  
**风险**：`permissive` 跳过 `model_invoked` 检查，允许未被调用的模型返回空结果被误判为通过。zero-cost 模式下不验证输出摘要是否为空字符串。

### MECH-03：异常隔离（planner_errors.py / lifecycle.py）

**机制**：`planner_errors.py` 定义 `PlannerError`、`RetryExhaustedError`、`RecoveryCapError` 等；`lifecycle.py` 按 error type 写入 `outcome`。  
**证据**：异常层次清晰，`lifecycle.py` 第 260-290 行捕获异常并持久化。  
**评价**：无显著风险。异常边界完整。

### MECH-04：warm handoff 上下文装配（execution.py）

**机制**：`execution.py` 中的 `build_warm_handoff()` 从数据库读取前序 artifact，拼接为上下文。  
**证据**：第 300-360 行。受 `warm_handoff_max_bytes` 和 `context_max_bytes` 双重约束。  
**风险**：当 `warm_cap + mandatory_bytes > context_max_bytes` 时仅输出 warning，不会截断 mandatory bytes。无人值守下若 mandatory bytes 过大，warm handoff 可能被静默丢弃，导致规划器上下文缺失关键历史。

### MECH-05：provider 调用抽象（provider.py）

**机制**：`provider.py` 定义 `call_llm()` 接口，适配 local / deepseek / openai 等厂商。  
**证据**：`provider_agent_text()` 从 JSONL 提取 assistant 消息；`call_llm_streaming()` 返回迭代器。  
**风险**：无重试/熔断机制。bridge 调用失败直接向上传播异常，无人值守时可能直接导致任务失败而非降级。

### MECH-06：host_bridge 通信层（host_bridge.py）

**机制**：`_bridge_configuration()` 读取环境变量构造 base_url 和 token；`_bridge_post()` 执行 HTTP POST。  
**证据**：`workspace_snapshot()` 和 `_bridge_post()` 等函数。  
**风险**：无超时参数硬编码；SSL 验证完全依赖底层 httpx 配置，未显式验证证书。无健康检查或 fallback 逻辑。

### MECH-07：result_ingest 结构化输出（result_ingest.py）

**机制**：将 LLM 自然语言输出解析为 `TaskResult` 结构体，包含 `acceptance_ids`、`summary`、`verify_commands`。  
**证据**：`__str__` 方法以 256 字符截断显示，但完整数据保持。  
**风险**：解析层无严格的 JSON Schema 校验，若 LLM 输出格式偏离，解析结果可能为空但被 accept（特别是在 `permissive` 验证模式下）。

---

## 无人值守/优质代码/省 Token 风险与建议

### 无人值守风险

1. **无限循环风险（高）**：`progress_policy.py` 的 `should_retry_planner()` 在 `RETRY_PLANNER` 匹配后返回 True，而 `planner.py` 重试循环仅在 `max_retries` 耗尽时退出。若 `max_retries` 被配置为 0 或未设置（默认值缺失），可能无限重试。**建议**：在 `planner.py` 的重试入口处添加硬上限断言，防止配置错误导致无限循环。

2. **静默上下文丢失（中）**：warm handoff 超过 `context_max_bytes` 时仅记录 warning，无人值守时不产生警报。任务后续可能在缺失前序证据的情况下执行。**建议**：在 `execution.py` 的 warning 逻辑后追加结构化日志事件，供外部监控系统捕获。

3. **验证降级不可见（中）**：`verification.py` 的 `permissive` 模式使用方未在调用处记录日志标记降级事件。无人值守时无法区分是故意宽松还是配置错误。**建议**：在 `verify_task_result()` 入口统一 emit 带 `verification_mode` 字段的日志事件。

### 优质代码优势

- 异常层次设计优良（`planner_errors.py`），与 lifecycle 配合紧密。
- 验证模式（strict/permissive/zero）覆盖多种成本策略，设计合理。
- result_ingest 的 `__str__` 截断（256 字符）避免日志爆炸，是务实的工程选择。
- warm handoff 机制复用已有 artifact，避免重复调用 LLM，节省 token。

### 省 Token 风险

1. **zero-cost 模式语义空洞（中）**：`verification.py` 的 zero 模式仅校验 `model_invoked=False` 且 token 为零，但不验证 `output_ref` 是否为空或摘要是否完整。可能出现：LLM 被调用但返回空字符串，被误判为零成本。**建议**：在 zero 模式下添加 `output_ref` 非空校验，或者要求显式声明 `output_ref` 为 null 而非空字符串。

2. **浅层 token 计数偏移（低-中）**：provider 层返回的 token 计数来自 LLM 厂商响应中的 `usage` 字段。`verification.py` 的 strict 模式校验 `input_tokens + cached_input_tokens ≤ budget`。若厂商返回的 token 计数不包含 system prompt 或工具定义，预算校验将偏移。**建议**：在 provider 层对 token 字段做归一化标注（如 `provider_tokens_estimated`），避免直接信任厂商数值。

3. **context 截断无语义感知（低）**：`execution.py` 的 warm handoff 装配按字节截断，非按语义结构（如截断最旧的 artifact 而非随机）。在长对话场景中可能丢弃关键决策记录。**建议**：引入优先级标记，允许 planner 为关键 artifact 设置 `retain_priority`，确保截断时优先保留高优先级内容。

### 综合建议优先级

| 优先级 | 建议 | 影响模块 |
|---|---|---|
| P0 | 重试循环添加硬上限断言 | planner.py |
| P1 | verification 模式切换 emit 结构化日志 | verification.py |
| P1 | zero-cost 模式增加 output_ref 非空校验 | verification.py |
| P2 | warm handoff 截断时保留高优先级 artifact | execution.py |
| P2 | provider 层增加 bridge 超时与重试 | provider.py, host_bridge.py |
| P3 | token 计数归一化标注 | provider.py |
