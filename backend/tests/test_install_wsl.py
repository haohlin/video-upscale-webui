from pathlib import Path


ROOT = Path(__file__).parents[2]


def test_wsl_service_is_loopback_only_and_uses_private_environment():
    service = (ROOT / "deploy/video-upscale-webui.service").read_text()

    assert "EnvironmentFile=/etc/video-upscale-webui/runtime.env" in service
    assert "--host 127.0.0.1" in service
    assert "--port 8000" in service
    assert "ProtectSystem=strict" in service
    assert "NoNewPrivileges=true" in service


def test_wsl_installer_preserves_models_and_pins_reviewed_seedvr2_fork():
    installer = (ROOT / "scripts/install-wsl-runtime.sh").read_text()

    assert 'SEEDVR2_NODE_REVISION="67a7350959eb077d3184faac7afa5449d8cc30a5"' in installer
    assert 'SEEDVR2_FORK_REPOSITORY="https://github.com/haohlin/ComfyUI-SeedVR2_VideoUpscaler.git"' in installer
    assert 'VIDEO_UPSCALE_SEEDVR2_MODEL_DIR="${VIDEO_UPSCALE_STATE_ROOT}/models/SEEDVR2"' in installer
    assert "rm -rf" not in installer
    assert "--with-7b" in installer
    assert "hf_hub_download" in installer
    assert "sha256" in installer.lower()
    assert "useradd --system" in installer
    assert 'VIDEO_UPSCALE_PYTHON_VERSION="3.13.12"' in installer
    assert 'UV_VERSION="0.10"' in installer
    assert '/home/linuxbrew/.linuxbrew/bin/uv' in installer
    assert '.local/share/uv/python/cpython-3.13-linux-x86_64-gnu/bin/python3.13' in installer
    assert 'python install "$VIDEO_UPSCALE_PYTHON_VERSION"' in installer
    assert 'runtime-requirements.cuda.lock' in installer
    assert "--require-hashes" in installer
    assert "--index-url https://download.pytorch.org/whl/cu128" not in installer
    assert "SELECT COUNT(*) FROM jobs" in installer
    assert "refusing runtime update while a queued or active job exists" in installer
    assert 'systemctl is-active --quiet "$SERVICE_NAME"' in installer
    assert 'systemctl stop "$SERVICE_NAME" 2>/dev/null || true' not in installer
    assert '--exclude node_modules' in installer
    assert '--exclude .venv' in installer
    assert 'systemctl start "$SERVICE_NAME"' in installer


def test_cuda_preflight_requires_4090_and_24gb_class_vram():
    preflight = (ROOT / "scripts/check-cuda-system.sh").read_text()

    assert "NVIDIA GeForce RTX 4090" in preflight
    assert "23000" in preflight
    assert "torch.cuda.is_available()" in preflight
    assert "nvidia-smi" in preflight
    assert 'VIDEO_UPSCALE_SEEDVR2_MODEL_DIR' in preflight
    assert 'VIDEO_UPSCALE_SEEDVR2_3B_MODEL' in preflight
    assert 'VIDEO_UPSCALE_SEEDVR2_7B_FP8_MODEL' in preflight
    assert 'VIDEO_UPSCALE_SEEDVR2_VAE_MODEL' in preflight


def test_wsl_runtime_template_uses_cuda_profiles_and_persistent_paths():
    runtime = (ROOT / "deploy/runtime.wsl.env.example").read_text()

    assert 'VIDEO_UPSCALE_DEVICE_BACKEND_CLASS="nvidia-cuda"' in runtime
    assert 'VIDEO_UPSCALE_DEFAULT_PROFILE="7b-fp8-quality"' in runtime
    assert 'VIDEO_UPSCALE_BACKEND_ID="windows-4090"' in runtime
    assert 'VIDEO_UPSCALE_ALLOWED_WEB_ORIGIN="https://' in runtime
    assert 'VIDEO_UPSCALE_SEEDVR2_MODEL_DIR="/var/lib/video-upscale-webui/models/SEEDVR2"' in runtime


def test_backend_has_frozen_production_requirements_for_wsl_install():
    requirements = (ROOT / "backend/requirements.lock").read_text()

    assert "--hash=sha256:" in requirements
    assert "fastapi==" in requirements
    assert "uvicorn==" in requirements


def test_cuda_smoke_submits_and_waits_for_a_real_backend_job():
    smoke = (ROOT / "scripts/smoke-cuda.sh").read_text()

    assert "testsrc2=size=256x256:rate=5:duration=1" in smoke
    assert 'preset="$1"' in smoke
    assert 'POST' in smoke
    assert 'Tailscale-User-Login' in smoke
    assert 'status' in smoke
    assert 'ffprobe' in smoke


def test_windows_tailscale_serve_script_is_private_and_exact_port_only():
    serve = (ROOT / "scripts/setup-windows-tailscale-serve.ps1").read_text()

    assert "tailscale serve" in serve
    assert "--https" in serve
    assert "127.0.0.1:8000" in serve
    assert "tailscale funnel" not in serve.lower()
    assert "8444" in serve
