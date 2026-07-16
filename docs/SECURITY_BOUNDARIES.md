# Security boundaries

- The server binds to loopback by default. A non-loopback bind refuses startup without `PLOW_WHIP_API_TOKEN`, requires Bearer authentication and rejects cross-origin requests.
- The generic provider receives an environment allowlist, not the parent environment. API keys, Bearer values and private-key blocks are redacted from captured output.
- Verification paths stay below the project root. Absolute command arguments outside the project and `../` traversal are rejected before claim.
- Generic commands execute as the container user. This is not a hostile-code sandbox; only trusted task payloads belong in this provider.
- Secrets are references, never exported values. Metadata export, diagnostics and audit omit command/request bodies.
- The embedded Cron engine can invoke only the fixed global Tick; the UI cannot create arbitrary shell crontab entries.
- Compose publishes only `127.0.0.1`. The container is unprivileged, drops all Linux capabilities, uses a read-only root filesystem, and writes only `/data`, `/projects` and tmpfs.
- The Host Bridge requires a high-entropy Bearer token, explicit project-root allowlists and one of three fixed adapters: Codex, Cursor or JSON Worker. It never accepts a shell string, arbitrary argv, environment map or out-of-root path.
- Codex runs with `workspace-write` and `approval_policy=never`; Cursor runs headless with its sandbox enabled. Neither adapter uses a bypass-sandbox flag. The bridge passes only a small environment allowlist.
- Provider configuration stores only an optional credential environment-variable name. Plaintext platform keys are not accepted by the Provider API or returned to the browser.
- Every mutating HTTP request is locally audited. Permission decisions are durable and revocable.
