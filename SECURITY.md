# Security Policy

## Supported deployment

Video Upscale WebUI is a single-operator, private service. Supported deployment binds Uvicorn and optional ComfyUI to loopback, publishes only Uvicorn through Tailscale Serve, keeps Funnel disabled, and restricts tailnet ACLs to the operator's devices.

Every UI and job route requires Tailscale Serve's verified `Tailscale-User-Login` header to match the configured operator. No local WebUI key, token, or fallback login exists. `/api/health` intentionally exposes only runner readiness for local monitoring. Local processes are trusted because they can reach the loopback listener and forge proxy headers.

Do not expose this service directly to LAN or public internet. Multi-user job isolation is outside current security model.

## Reporting a vulnerability

Use GitHub's private vulnerability-reporting or Security Advisory flow. Do not include access tokens, private videos, runtime logs, tailnet names, or local configuration in a public issue.

Include affected revision, source location, supported deployment assumptions, and minimal reproduction steps using synthetic media.
