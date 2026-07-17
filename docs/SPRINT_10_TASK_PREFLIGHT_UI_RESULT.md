# Sprint 10 Task Preflight UI Result

Status: COMPLETE

## Delivered

- The create-task drawer now requires `POST /api/tasks/estimate` before enqueue and labels the preview as a 0 Token rule evaluation.
- The structured request includes layers, components, files, migration/deploy flags, verification count/time, external dependencies, risk, independent review, and all four dispatch gates.
- Any sizing input change immediately removes the old preview and disables enqueue. An estimate API failure also clears the prior success, including responses that finish after an input change.
- `needs_planning` and missing gates block enqueue with a concrete split-or-complete-gates message; no budget override can bypass the block.
- The preview card renders the server-provided size class, input/output token bands and p90, reserved tokens, estimated hard cap, dynamic deadlines, turns, attempts, verification timeout, rationale summary, and `model_invoked=false` fact.
- Successful creation sends the same `sizing_inputs`; the returned `TaskView` remains authoritative for persisted sizing and execution budget.
- The existing UI had no manual token override, so this slice did not add one. The temporary bootstrap cap was not promoted into UI logic or defaults.

## Files

- `web/src/App.tsx`
- `web/src/api.ts`
- `web/src/styles.css`
- `web/src/App.test.tsx`
- `docs/SPRINT_10_TASK_PREFLIGHT_UI_RESULT.md`

No backend, migration, package, lockfile, deployment, commit, or push was performed. Existing unrelated dirty changes were preserved.

## Verification

- `cd web && pnpm test` — PASS, 1 file / 10 tests.
- `cd web && pnpm run typecheck` — PASS.
- `cd web && pnpm run lint` — PASS.
- `cd web && pnpm run build` — PASS, Vite production bundle generated.
- `git diff --check` — PASS.

SPRINT10_TASK_PREFLIGHT_UI_COMPLETE
