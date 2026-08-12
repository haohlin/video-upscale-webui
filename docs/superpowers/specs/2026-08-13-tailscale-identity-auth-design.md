# Tailscale Identity Authentication Design

## Goal

Remove local HTTP Basic credentials and authorize the single operator through Tailscale Serve identity only.

## Trust boundary

- FastAPI continues listening only on `127.0.0.1`.
- Tailscale Serve remains the only remote entry point; Funnel remains disabled.
- Every route except `/api/health` requires `Tailscale-User-Login` to equal the configured operator login, compared case-insensitively after ASCII whitespace trimming.
- The deployed operator login is `haohan.apple@outlook.com`.
- Missing, malformed, tagged-device, or different-user identity returns `403` without a Basic-auth challenge.
- Local processes on the Mac are inside the trust boundary because loopback callers can forge proxy headers. LAN and direct tailnet connections cannot reach the loopback listener.
- State-changing routes retain `X-Video-Upscale-Request: 1` CSRF defense.

## Runtime migration

- Remove access username/token settings, token generation, token-file checks, and token documentation.
- During an applied runtime update, remove only the exact legacy configured access-token file after the service is quiesced.
- Validate that Tailscale is connected, Serve targets the loopback backend, Funnel is absent, and an operator login is configured.

## Verification

- Focused tests cover missing, wrong, and correct Tailscale identity; health; mutation header; no Basic challenge; and removal of token generation/configuration.
- The cross-repository release gate remains at or below 49 cases.
- Deployment proves loopback binding, private Serve status, correct identity authorization through the tailnet URL, and one accepted short 1x job.

