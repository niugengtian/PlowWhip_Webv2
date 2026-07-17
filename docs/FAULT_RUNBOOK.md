# Fault runbook

- `offline`: keep local tasks running; network tasks remain ready. Do not force retries.
- `domestic_only` or `overseas_only`: run only compatible tasks.
- `skipped_database_busy`: wait for the next OS tick; never add an inner retry loop.
- `needs_human`: read the outbox event, solve the one stated blocker, then resume.
- stale `running`/`verifying`: Recovery reclaims only missing or expired leases with no unconsumed Host Job and emits `task.recovered`.
- `recovery_hold`: restore Host Bridge reachability and run one Tick. Do not manually requeue while the old PID is unconfirmed.
- `orphan_running`: the Bridge restarted while the CLI child survived. Let it finish or request cancel; never start a second worker for that task.
- `stopping`: cancellation is waiting for host TERM/KILL confirmation. The worker and resource lock remain held until confirmation.
- repeated identical evidence: the same-failure/no-progress counters terminate at the configured threshold.
- machine sleep: a persistent timer resumes with one bounded tick; missed tick history is not replayed.
- provider unavailable: configure that adapter or change the task provider. The task remains ready and attempts remain zero.
