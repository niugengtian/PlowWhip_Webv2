# Sprint 2 acceptance evidence

Date: 2026-07-17

## Delivered

- Multiple registered projects with five practical roles: coordination, fullstack, web3, devops_sre, verification.
- Durable `project -> role -> worker -> provider session_id` binding.
- Worker session reuse inside one project/role and hard isolation across projects.
- Atomic task leases, fencing tokens and cross-project resource locks.
- One active task per RoleWorker; project completion archives and releases its sessions.
- Projects and Workforce web views.

## Verification

- Backend: 12 tests passed, 87% coverage.
- Frontend: Vitest, TypeScript, ESLint and production build passed.
- Concurrency acceptance runs two projects in parallel.
- Brain-split acceptance rejects both a second task on a busy worker and a cross-project collision on `port:3000`.
- Release acceptance leaves zero task leases and zero resource locks while retaining a session archive.

The generic command provider reports zero model tokens. Control-plane scheduling is still explicit in this sprint; automatic zero-token tick arrives in Sprint 3.
