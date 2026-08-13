param(
    [switch]$Apply,
    [int]$HttpsPort = 8444,
    [int]$BackendPort = 8000
)

$ErrorActionPreference = "Stop"
$tailscale = Get-Command tailscale -ErrorAction Stop
$status = & $tailscale.Source status --json | ConvertFrom-Json
if ($status.BackendState -ne "Running") {
    throw "Tailscale is not running"
}

$target = if ($BackendPort -eq 8000) { "http://127.0.0.1:8000" } else { "http://127.0.0.1:$BackendPort" }
if (-not $Apply) {
    Write-Output "DRY RUN: tailscale serve --https=$HttpsPort --bg $target"
    exit 0
}

# Serve is visible only to authenticated tailnet peers. This script deliberately
# configures one HTTPS listener and never enables public exposure.
& $tailscale.Source serve "--https=$HttpsPort" --bg $target
if ($LASTEXITCODE -ne 0) {
    throw "Could not configure private Tailscale Serve"
}
& $tailscale.Source serve status
