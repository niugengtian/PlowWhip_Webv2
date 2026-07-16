# Sprint 8 — Worker Provider Pool

## Outcome

- Platform API keys are optional; the system boots and completes Generic Command tasks without one.
- Codex CLI, Cursor CLI and simple-worker register through a restricted Host Bridge.
- Project → role → provider → CLI external session binding is durable and project-scoped.
- Idle workers can be explicitly rotated/rebound; provider switching is never implicit.
- The global scheduler probes enabled Providers with a 0 Token operation.
- Convention refinement records usage, returns a suggestion and requires explicit adoption/save.
- The Web UI is a Chinese, dark, high-density Edict-inspired control console.

## Security boundaries

- No arbitrary shell or argv API.
- Host project roots are allowlisted.
- Host Bridge authentication is mandatory.
- Provider credentials are environment references only.
- CLI failure becomes normal task evidence and releases leases.

## Verification

- Backend tests cover migration, revision guards, binding/rebinding, refinement non-overwrite and fixed bridge argv.
- Frontend typecheck, lint, test and production build are release gates.
- Docker health, persistence and browser Design QA are recorded before release commit.
