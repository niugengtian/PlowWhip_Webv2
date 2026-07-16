# Security boundaries

- The server binds to loopback by default. A non-loopback bind refuses startup without `PLOW_WHIP_API_TOKEN`, requires Bearer authentication and rejects cross-origin requests.
- The generic provider receives an environment allowlist, not the parent environment. Common API keys, Bearer values and private-key blocks are redacted from captured output.
- Verification file paths must remain below the project root. Absolute command arguments outside the project and `../` traversal arguments are rejected before a task is claimed.
- Generic local commands still execute with the current operating-system user. This MVP is not a hardened hostile-code sandbox; only trusted local command payloads should be approved.
- Secrets are references, never exported values. Metadata export, diagnostics and audit deliberately omit command bodies and request bodies.
- OS scheduler installation requires a persisted explicit setting and writes only a current-user launchd/systemd/Task Scheduler definition.
- Every mutating HTTP request is locally audited. Permission decisions are durable and revocable.
- Non-loopback browser use currently expects an authenticated local client or reverse proxy to attach the Bearer token; no cookie authentication is used, avoiding ambient-cookie CSRF.
