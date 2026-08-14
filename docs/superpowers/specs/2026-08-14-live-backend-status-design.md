# Live Backend Status Design

## Goal

Keep processing-host selection truthful while showing live Mac and Windows resource status. Windows CUDA jobs use SeedVR2 7B Quality by default and skip the Apple-only 7B safety preflight.

## Design

Each backend's existing `/api/health` response includes a bounded `metrics` object: sampled time, CPU percent, RAM used/total, GPU percent, and GPU memory used/total. Linux reads `/proc` and `nvidia-smi`; macOS reads Mach CPU counters, `vm_stat`, and AGX `ioreg`. Missing metrics are `null` and never make health fail.

The frontend polls every configured backend every two seconds. The processing-host selector contains Auto plus ready backends only. A separate Machine Status section always contains every configured backend, including offline machines, and renders the newest metrics. Auto prefers the ready backend with the lowest configured preference.

The Mac service reaches the Windows backend through Tailscale Serve inside WSL, avoiding Windows-to-WSL localhost forwarding. CUDA 7B jobs go directly to full inference; Apple MPS 7B jobs retain the limited preflight.

## Verification

Use focused backend and frontend contract tests, the capped release gate, frontend build, live Mac and Windows health probes, and one real 7B Quality 1x upload of `大堡礁3-clip1.mp4` through the resumable WebUI API. Validate the completed output with ffprobe.
