# Sprint 10 Host Output 持久化结果

日期：2026-07-17

## 工作树边界

本次只修改 `backend/plow_whip_web/host_bridge.py`、`backend/plow_whip_web/store/host_job_repository.py`、`tests/test_host_job_continuity.py` 和本结果文件。`backend/plow_whip_web/providers/host_bridge.py` 的既有 `HostBridgeClient.result()` 已自然消费短 `stdout`/`stderr` tail，不需要修改。

开始时已有的脏文件 `api/schemas.py`、`domain/model.py`、`runtime/budget.py`、`runtime/context.py`、`store/settings_repository.py`、`store/task_repository.py` 和迁移 `0015_model_call_accounting.sql` 均未修改、覆盖或回滚。没有新增数据库迁移，没有提交或推送。

## 机制

- `HostJobManager` 对收到的每一行先执行 `Redactor.redact()`，再以 UTF-8 字节追加到 `state-dir/<job_id>/stdout.NNNNNN.log` 或 `stderr.NNNNNN.log`。
- 单段上限为 262144 bytes。正常输出按完整行在写入前判断并轮转；单行自身超过上限时只在 UTF-8 字符边界拆分，保证无损且所有段都不越界。段文件只追加，创建权限为 `0600`，job 目录为 `0700`，每次追加 flush/fsync。
- job JSON 继续是唯一生命周期状态机，同时保存 `output_ref`、有序 `output_segments`（stream、index、ref、bytes、SHA-256）和分流/总 `output_bytes`。状态 JSON 和 carry-forward 都通过同目录临时文件加原子 `os.replace()` 更新。
- 内存和 API snapshot 的 `stdout`、`stderr` 各自只保留最后 16384 bytes 的有效 UTF-8 tail。完整流不再进入 snapshot。
- manager 重建时扫描既有 segment 文件、重新计算大小和 SHA-256、重建 tail 和索引；若发现旧格式 job JSON 且尚无 segment，则先把其中已有的脱敏 tail 搬入 segment，保留升级兼容。
- `completed`、`cancelled`、`interrupted`（包括 timeout 对应的 completed/failure_class=timeout）统一由现有 `_write()` 终态路径生成确定性 `carry-forward.json`。内容包含状态、失败类、session_id、输入/输出/总 Token、最后有效 stdout/stderr、segment 引用/大小/哈希和 `generation_model_tokens: 0`，不调用模型。
- `HostJobRepository.record()` 对 SQLite `result_json` 使用字段白名单；输出仅保存脱敏短 tail、错误摘要、总字节数、segment 索引/哈希/引用和任务生命周期字段。即使 segment 输入夹带 `content`，也会丢弃，不能把完整输出伪装回 SQLite。

## 改动文件

- `backend/plow_whip_web/host_bridge.py`：脱敏文件流、轮转、索引/哈希、重建恢复、短 tail、终态 carry-forward。
- `backend/plow_whip_web/store/host_job_repository.py`：SQLite snapshot 白名单和 UTF-8 短 tail。
- `tests/test_host_job_continuity.py`：先添加失败测试，覆盖超过两个轮转段的无损重组、段上限/hash、秘密脱敏、manager 重建、timeout carry-forward 确定性和 SQLite 大字段隔离。
- `docs/SPRINT_10_HOST_OUTPUT_RESULT.md`：本证据记录。

## SQLite 大小证据

repository 测试注入超过 `3 * 262144 = 786432` bytes 的 stdout，并在 segment 元数据中额外夹带同一份完整 `content`。断言证明：

- `result_json` UTF-8 大小小于 32768 bytes，显著小于单个旧上限 262144 bytes，也小于注入原文的约八分之一；
- SQLite 中的 stdout tail 不超过 16384 bytes；
- 旧输出头部 sentinel 不存在于 `result_json`；
- segment 的 `content` 字段被丢弃；
- `output_ref`、精简后的 `output_segments` 和 `output_bytes` 仍完整保留。

## 测试命令与结果

按要求只运行：

```text
.venv/bin/python -m pytest tests/test_host_job_continuity.py -q
```

先写测试后的实现前结果：测试收集失败，`ImportError: cannot import name 'MAX_OUTPUT_TAIL_BYTES'`，证明新机制尚不存在。

实现后的两次结果一致：

```text
14 passed, 3 failed
```

本次新增的三项系统测试全部在 14 个通过项中：大于两个 segment 的真实进程输出/重建、确定性 timeout carry-forward、SQLite 大字段隔离。剩余三个失败均在 Host 任务完成后的既有调用链，错误完全相同：

```text
TypeError: TaskRepository.finish() got an unexpected keyword argument 'max_no_progress'
```

失败测试为：

- `test_host_budget_reservation_prevents_global_oversubscription`
- `test_container_reconciles_completion_and_retains_active_lease`
- `test_host_verification_uses_host_project_path`

当前 `runtime/task_service.py` 仍传入 `max_no_progress`，而被明确禁止修改的当前脏 `store/task_repository.py` 已删除该参数。该契约不一致不属于 Host 输出迁移，且无法在本任务边界内修复。

## 完成状态

Host 输出迁移目标的新增验证已通过，但指定测试命令尚未全绿。根据“Only verification evidence can move this task to completed”，当前不能宣告 Sprint 10 完成，也不能写入完成标记。待拥有这些脏文件的工作将 `TaskService`/`TaskRepository.finish()` 契约对齐后，应原样复跑上述唯一测试命令；只有得到退出码 0，才能在本文件末尾追加要求的 `SPRINT10_HOST_OUTPUT_COMPLETE`。

SPRINT10_HOST_OUTPUT_BLOCKED
