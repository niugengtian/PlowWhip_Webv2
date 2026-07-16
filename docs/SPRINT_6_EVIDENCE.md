# Sprint 6 release acceptance evidence

Date: 2026-07-17

## Delivered

- Loopback-default serving; non-loopback startup requires a Bearer token and rejects cross-origin requests.
- Provider registry with honest unavailable states for unconfigured model CLIs.
- Project-root command argument policy, environment allowlist and output Secret redaction.
- Durable permission decisions and mutation audit without request bodies or command bodies.
- Fast, Balanced and Strict bounded quality profiles; Strict performs exactly one independent deterministic review.
- Providers, Audit, Health, permissions and maintenance product surfaces in addition to all previous pages.
- Integrity-checked backup, metadata export, diagnostic ZIP and confirmed restore with safety backup.
- Source one-command start and self-contained wheel builder that embeds the production Web UI.
- Architecture, security boundary, operations, fault, acceptance and release documentation.

## Automated gates

- Backend: 147 tests passed with 88% branch-aware coverage and zero warnings.
- Fault injection: 100 bounded cases plus crash, duplicate, old revision, lease, lock, network and no-progress tests.
- Frontend: Vitest, TypeScript, ESLint and Vite production build passed.
- Dependencies: Python `pip check` reported no broken requirements; production `pnpm audit` reported no known vulnerabilities.
- Real HTTP E2E served the built homepage and completed one Web3 and one ordinary IT Strict task in parallel, both with verification hashes and `control_tokens: 0`.
- Wheel build passed; archive inspection proved it contains all seven migrations and the hashed HTML/CSS/JS production UI.
- OpenAPI includes control, maintenance, permissions, providers, audit, recovery and scheduler surfaces.

## Browser acceptance

- Real in-app browser checked the live FastAPI-served production build.
- Desktop full-page layout and 390 × 844 responsive layout were visually inspected.
- Today, Settings and Health rendered their live API state.
- Recovery button returned an explicit zero-model result.
- Browser console reported zero warnings and zero errors.

## Security review

- Reviewed subprocess, filesystem write, restore, scheduler-install, authentication, Origin, Secret and project-root sinks.
- Cross-project absolute arguments and traversal are rejected before task claim.
- Model provider absence leaves the task ready with zero attempts; it cannot fake completion.
- The built-in command provider is for trusted local commands, not hostile-code isolation. That boundary is explicit in `SECURITY_BOUNDARIES.md`.

## Exit result

`pass`

The actual user-level OS scheduler was not installed during acceptance because it writes outside the authorized project directory. The product exposes the detected launchd plan and records authorization in Settings and the permission ledger before installation.
