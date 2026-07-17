# Sprint 10 — Host Timeout Continuation Result

## Objective

Host CLI 墙钟超时且 snapshot 为 `status=completed` + `failure_class=timeout` 时，不得进入 completed→verification→terminal_failed；应路由到 `resume_after_external_interruption`，保留 CLI Session 并退回 READY 续接。

## Actual Diff (this slice)

### `backend/plow_whip_web/runtime/task_service.py`

在 `reconcile_host_jobs` 的 `status == "completed"` 分支入口新增 timeout 路由：

```python
failure_class = str(snapshot.get("failure_class") or "")
if (
    failure_class == "timeout"
    and task.status in {TaskStatus.RUNNING, TaskStatus.VERIFYING}
):
    result = self.repository.resume_after_external_interruption(
        task_id, job_id=job_id,
        external_session_id=snapshot.get("session_id"),
    )
    self._settle_host_reservation(task, job, snapshot)
    self.host_jobs.consume(job_id)
    settled.append({"task_id": task_id, "status": result.status.value})
    continue
```

行为要点：

- 不调用 `verify_host_task` / `_finish_execution`
- 结算本次真实 Token（`_settle_host_reservation`）后 consume 旧 job
- `session_id` 经 `resume_after_external_interruption` 写回 worker；缺失时 COALESCE 保留既有绑定
- `command_failed` / `verification_failed` 仍走原 completed 验证路径

工作树中保留的上一任务契约修复（未在本切片改动）：`_finish_execution` 移除 `max_no_progress` 传参。

### `tests/test_host_job_continuity.py`

- `FakeAsyncBridge` 增加 `verify_calls`、`start_sessions` 计数
- 新增 `test_timeout_completed_snapshot_resumes_same_session_without_verification`
- 新增 `test_command_failed_completed_still_runs_verification_path`

## Regression Coverage

| # | 断言 | 测试 |
|---|------|------|
| 1 | timeout snapshot 不调用 verifier | `verify_calls == 0` |
| 2 | 任务 READY、`attempts_used` 不增加、旧 job consumed | `status=ready`, `attempts_used=0`, `active()==[]` |
| 3 | worker `external_session_id` 保留为 `timeout-session` | worker 查询 |
| 4 | 下一次 drive/start_job 收到同一 `session_id` | `start_sessions[-1] == "timeout-session"` |
| 5 | 同一 timeout job 重放幂等，不重复 Token 结算 | `consumed_at` 重置后 reconcile，`token_usage` 仍为 1 |
| 6 | 普通 `command_failed` 仍走验证/失败逻辑 | `verify_calls >= 1`, `attempts_used == 1` |

## Test Evidence

```text
$ .venv/bin/python -m pytest tests/test_host_job_continuity.py -q
...................                                                      [100%]
19 passed

$ git diff --check
(no output — clean)
```

## Files Touched (allowed scope only)

- `backend/plow_whip_web/runtime/task_service.py`
- `tests/test_host_job_continuity.py`
- `docs/SPRINT_10_TIMEOUT_CONTINUATION_RESULT.md`

SPRINT10_TIMEOUT_CONTINUATION_COMPLETE
