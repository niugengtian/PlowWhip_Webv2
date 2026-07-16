# Acceptance matrix

The 29 reviewed acceptance requirements are mapped to automated or explicit authorization evidence.

| # | Evidence |
|---:|---|
| 1–4 | task state, terminal absorption, verification and idempotency tests |
| 5–7 | multi-project concurrency, role-worker session isolation and release tests |
| 8–10 | global scheduler lease, fencing and zero-token tick tests |
| 11–13 | Settings revision, embedded Cron validation, persistent slot deduplication and Docker runner heartbeat tests |
| 14–16 | three-scope Convention, five role templates and bounded Context Pack tests |
| 17–19 | token preflight, usage ledger and JSONL rotation/hash tests |
| 20–22 | pause/resume/cancel, outbox/SSE and needs-human tests |
| 23–25 | four network states, flight mode and sleep/resume tests |
| 26 | stale lease/task/worker recovery and database-lock safe-skip tests |
| 27 | 100 fault-policy injection cases and identical-evidence loop guard test |
| 28 | authentication, Origin, project-root argument, redaction, provider-unavailable and audit tests |
| 29 | two-project Web3/IT strict E2E, OpenAPI, frontend build and real HTTP release E2E |

Sprint 7 additionally proves the real Docker image, loopback-published container, non-root runtime, named-volume persistence, restart recovery and live Crontab UI. No host scheduler installation, publish or external Secret transfer is performed.
