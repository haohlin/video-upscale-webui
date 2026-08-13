import os
from pathlib import Path
import subprocess


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_runtime_loader_exports_optional_backend_registry():
    library = (PROJECT_ROOT / "scripts/lib.sh").read_text()

    assert "VIDEO_UPSCALE_BACKENDS_JSON" in library
    assert "VIDEO_UPSCALE_ALLOWED_WEB_ORIGIN" in library
    assert 'export "$optional_name"' in library


def run_config_loader(config: Path, command: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.pop("VIDEO_UPSCALE_TAILSCALE_USER_LOGIN", None)
    environment["VIDEO_UPSCALE_CONFIG_FILE"] = str(config)
    return subprocess.run(
        [
            "zsh",
            "-c",
            f'source "{PROJECT_ROOT / "scripts/lib.sh"}"; load_runtime_config; {command}',
        ],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def test_frontend_build_always_reinstalls_lock_derived_dependencies():
    """Existing node_modules must never bypass npm lock installation."""
    script = (PROJECT_ROOT / "scripts/start-local.sh").read_text()

    assert "npm ci --ignore-scripts" in script
    assert "[[ -d node_modules ]] || npm ci" not in script


def test_runtime_checkout_verifies_origin_and_clean_worktree():
    """A matching HEAD label must not authenticate modified runtime bytes."""
    script = (PROJECT_ROOT / "scripts/install-runtime.sh").read_text()

    assert "remote get-url origin" in script
    assert "status --porcelain --untracked-files=all" in script
    assert "unexpected submodules" in script
    assert "unexpected custom nodes" in script
    assert "runtime checkout contains ignored Python source" in script
    assert "uv sync --locked --reinstall" in script
    assert "uv pip install --reinstall" in script


def test_runtime_pins_reviewed_seedvr2_fork_and_upstream_provenance():
    script = (PROJECT_ROOT / "scripts/install-runtime.sh").read_text()

    assert 'SEEDVR2_NODE_REVISION="67a7350959eb077d3184faac7afa5449d8cc30a5"' in script
    assert "https://github.com/haohlin/ComfyUI-SeedVR2_VideoUpscaler.git" in script
    assert "https://github.com/numz/ComfyUI-SeedVR2_VideoUpscaler.git" in script
    assert "remote get-url upstream" in script
    assert '[[ "$actual_upstream" == "$expected_upstream" ]]' in script


def test_runtime_verifies_pinned_fork_descends_from_reviewed_upstream_base():
    script = (PROJECT_ROOT / "scripts/install-runtime.sh").read_text()

    assert 'SEEDVR2_UPSTREAM_REVISION="4490bd1f482e026674543386bb2a4d176da245b9"' in script
    assert "fetch --depth=64 origin" in script
    assert 'merge-base --is-ancestor "$upstream_revision" "$revision"' in script


def test_runtime_update_quiesces_before_any_apply_mutation():
    script = (PROJECT_ROOT / "scripts/install-runtime.sh").read_text()
    gate_script = (PROJECT_ROOT / "scripts/runtime-update-gate.py").read_text()
    gate = script.index("runtime-update-gate.py")
    first_mutation = min(
        script.index('run mkdir -p'),
        script.index("uv venv --clear"),
        script.index("fetch --depth=1"),
    )

    assert gate < first_mutation
    assert 'BLOCKING_STATUSES = ("queued", "preflight", "running")' in gate_script
    assert script.index("--check-only") < script.index("launchctl bootstrap")


def test_runtime_config_requires_tailscale_operator_login(tmp_path):
    config = tmp_path / "runtime.env"
    config.write_text(
        "\n".join(
            line
            for line in (PROJECT_ROOT / "deploy/runtime.env.example")
            .read_text()
            .splitlines()
            if not line.startswith("VIDEO_UPSCALE_TAILSCALE_USER_LOGIN=")
        )
    )

    result = run_config_loader(config, "true")

    assert result.returncode != 0
    assert "runtime config missing VIDEO_UPSCALE_TAILSCALE_USER_LOGIN" in result.stderr


def test_runtime_config_exports_tailscale_operator_login(tmp_path):
    config = tmp_path / "runtime.env"
    config.write_text(
        (PROJECT_ROOT / "deploy/runtime.env.example").read_text()
        + '\nVIDEO_UPSCALE_TAILSCALE_USER_LOGIN="operator@example.com"\n'
    )

    result = run_config_loader(
        config,
        "printenv VIDEO_UPSCALE_TAILSCALE_USER_LOGIN",
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "operator@example.com"


def test_runtime_scripts_have_no_local_browser_credential_path():
    runtime_sources = "\n".join(
        (PROJECT_ROOT / path).read_text()
        for path in [
            "scripts/lib.sh",
            "scripts/install-runtime.sh",
            "scripts/check-system.sh",
            "deploy/runtime.env.example",
        ]
    )

    assert "VIDEO_UPSCALE_ACCESS_USERNAME" not in runtime_sources
    assert "VIDEO_UPSCALE_ACCESS_TOKEN" not in runtime_sources
    assert "openssl rand" not in runtime_sources


def test_runtime_update_removes_only_exact_legacy_token_after_quiesce():
    script = (PROJECT_ROOT / "scripts/install-runtime.sh").read_text()
    gate = script.index("runtime-update-gate.py")
    removal = script.index('run rm -f -- "$legacy_access_token_file"')
    first_runtime_rebuild = script.index("uv venv --clear")

    assert 'legacy_access_token_file="${VIDEO_UPSCALE_DATA_ROOT}/access-token"' in script
    assert gate < removal < first_runtime_rebuild


def test_runtime_install_recreates_both_virtual_environments():
    """Arbitrary ignored Python must not survive a dependency reinstall."""
    script = (PROJECT_ROOT / "scripts/install-runtime.sh").read_text()

    assert script.count("uv venv --clear") == 2


def test_launchagent_install_removes_legacy_automatic_cleanup_schedule():
    """A stale cleanup plist must not reactivate after login or reboot."""
    script = (PROJECT_ROOT / "scripts" / "install-launchagents.sh").read_text()

    assert 'rm -f "$legacy_cleanup_destination"' in script
    assert 'install_one "com.haohanl.video-upscale-webui.cleanup"' not in script
