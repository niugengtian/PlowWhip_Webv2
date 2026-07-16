# Fault runbook

- `offline`: keep local tasks running; network tasks remain ready. Do not force retries.
- `domestic_only` or `overseas_only`: run only compatible tasks.
- `skipped_database_busy`: wait for the next OS tick; never add an inner retry loop.
- `needs_human`: read the outbox event, solve the one stated blocker, then resume.
- stale `running`/`verifying`: Recovery reclaims only missing or expired leases and emits `task.recovered`.
- repeated identical evidence: the same-failure/no-progress counters terminate at the configured threshold.
- machine sleep: a persistent timer resumes with one bounded tick; missed tick history is not replayed.
- provider unavailable: configure that adapter or change the task provider. The task remains ready and attempts remain zero.
