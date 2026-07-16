# Sprint 4 acceptance evidence

Date: 2026-07-17

## Delivered

- Global, project and task Convention scopes with optimistic revisions and an editor in Settings.
- Five concise practical role prompts only: coordination, fullstack, web3, devops_sre and verification.
- Deterministic Context Compiler that layers objective, role and three Convention scopes without replaying full chat history.
- UTF-8-safe context byte ceiling, content hash, immutable context-pack files and database index.
- Task and global daily Token gates checked before claiming a worker or invoking a provider.
- Immutable Token ledger with global, project and task views; scheduler/control usage is reported separately as zero.
- Per-worker JSONL session journals with size-based numbered rotation, SHA-256 archive evidence and atomic carry-forward metadata.
- Explicit idle-only CLI session rotation while retaining the same project/role Worker identity and archiving the old session.

## Why this saves Token

The scheduler only reads indexed state and uses no model. A worker receives a bounded compiled context instead of full conversation history. Stable project/role sessions are reused, while journals rotate outside the prompt. Convention content is layered once and hashed. Budget checks happen before provider invocation.

## Verification

- 23 backend tests passed with 86% coverage.
- Three-scope ordering, deterministic hashes and one-pack deduplication passed.
- Oversized Chinese UTF-8 context was capped without corrupting characters.
- A budget-overrun attempt was rejected while the task remained `ready`, attempts stayed zero and provider execution count stayed zero.
- Work-token ledger recorded actual provider tokens while control tokens remained zero.
- Journal rotation produced a numbered archive, matching SHA-256 and a new current file.
- Worker session generation rotated from 1 to 2, then project release archived both generations and cleared leases.
- Frontend Vitest, TypeScript, ESLint and production build passed.
