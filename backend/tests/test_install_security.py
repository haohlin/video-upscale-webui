from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


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


def test_runtime_install_recreates_both_virtual_environments():
    """Arbitrary ignored Python must not survive a dependency reinstall."""
    script = (PROJECT_ROOT / "scripts/install-runtime.sh").read_text()

    assert script.count("uv venv --clear") == 2
