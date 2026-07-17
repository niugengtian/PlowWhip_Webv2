# Sprint 11 Context Budget P0 Result

- Task: `079dcbaf-6e47-4898-b3e0-e4e92d20b7ab`
- Worker: `6b4eeb52-ffc1-441a-9a20-029504252f78`
- Date: 2026-07-18
- Original task status: `terminal_failed` (`budget_exceeded`)
- Final source status: VERIFIED AFTER THE CONFLICTING WRITER STOPPED
- Delivery actions: no deploy, no commit, no push

The original Host Job ended at `2026-07-17T17:12:39Z`. It intentionally
omitted the completion marker after detecting that another session had changed
the same files during its final audit. The control plane therefore preserved a
truthful failed terminal state. A later out-of-band resume changed the worktree
after that terminal event; it did not retroactively make the Host Job succeed.
The release source was frozen and independently re-verified only after all
writers stopped.

## Corrected interpretation

The observed turn reported 5,316,377 input tokens and 4,939,008 cached-input tokens. QA also found 33 command outputs totaling about 342 KB. Cached input is a subset of input, so accounting and hard-cap settlement use `input + output`; cached input is never added again.

These numbers do not identify 4,939,008 wasted tokens. Current Provider telemetry is aggregated at `turn` granularity and its value classification is `unknown`. Repeated/overlapping reads are candidates for investigation, but the available data cannot determine which content the model used or quantify valueless tokens. Likewise, `uncached_input_tokens = input_tokens - cached_input_tokens` means cache miss only; it does not mean new work, valuable work, or waste. The objective is to eliminate provably valueless loops while preserving valuable context, not to reduce total Token volume mechanically.

## Delivered mechanism

1. Provider input, cached-input, uncached-input, output, task, Worker, session generation, attribution granularity, value classification, and rotation reason are persisted as structured metadata. Prompt/stdout/stderr/key bodies are not stored in SQLite.
2. Worker state and API/UI expose last context pressure, guard decision, estimated new-work reserve, cached carry-in, hard cap, and relation. With only turn-level attribution, the relation is honestly `unknown` and the external session is reused.
3. Cached/context-pressure values are telemetry only. The former editable Provider pressure threshold and projected cached-plus-reserve rotation mechanism were deleted from runtime code, schemas, settings, UI, tests, and operating documentation. Old settings JSON is normalized to known `DEFAULT_SETTINGS` keys on read and update, so deprecated keys are ignored and removed on the next write.
4. External session rotation is limited to provable policies: actual settlement `input + output` hard-cap breach, bounded consecutive no-progress/tool-abort policy, or explicit operator rotate/rebind. Settlement rotation is transactionally idempotent; repeated reconciliation/ticks do not add another generation or archive. Journal byte limits rotate the local file only.
5. Explicit capacity/rate-limit/429/overload responses are classified as `provider_capacity`, enter bounded defer, and retain the session. Repeated no-progress is still decided by the unified FaultPolicy threshold.
6. The UI calls the hard cap a settlement gate and does not claim mid-turn cancellation. Reliable content compression and content-level value attribution remain unimplemented, so high-value large context is conservatively retained.

## Verification evidence

- Backend targeted context-budget/settings/FaultPolicy/Provider tests: 53 passed.
- Backend full suite: 255 passed.
- Frontend full test: 20 passed.
- Frontend typecheck, lint, and production build: passed.
- Fresh migration: 20 migrations, ending at `0020_provider_context_pressure.sql`; immediate second migration returned `[]`; SQLite health reported WAL and 20 migrations.
- Database migration/body-scrub suite: 3 passed.
- Final source audit: removed pressure-setting name and obsolete projected-rotation reasons have zero matches in runtime code, docs, and tests.
- Task detail loading rejects stale event/artifact responses after the operator
  changes selection, preventing facts from two tasks from being rendered
  together.
- `git diff --check`: passed.

# Marker for the final frozen release source only; the historical task remains
# terminal_failed in SQLite.
SPRINT11_CONTEXT_BUDGET_COMPLETE
