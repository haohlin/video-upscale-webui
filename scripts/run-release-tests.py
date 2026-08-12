#!/usr/bin/env python3
"""Run count-bounded, exact-name release tests across WebUI and SeedVR2 fork."""

from __future__ import annotations

import argparse
import ast
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import tomllib


GROUPS = ("backend", "frontend", "fork")


class GateError(RuntimeError):
    pass


def run(command: list[str], *, cwd: Path, capture: bool = False) -> str:
    result = subprocess.run(
        command,
        cwd=cwd,
        check=False,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )
    if result.returncode:
        detail = ""
        if capture:
            detail = "\n" + (result.stdout or "") + (result.stderr or "")
        raise GateError(f"command failed ({result.returncode}): {' '.join(command)}{detail}")
    return result.stdout if capture else ""


def load_manifest(path: Path) -> tuple[int, dict[str, list[str]]]:
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    maximum = data.get("max_tests")
    if type(maximum) is not int or maximum <= 0 or maximum > 49:
        raise GateError("max_tests must be an integer from 1 through 49")
    selected: dict[str, list[str]] = {}
    for group in GROUPS:
        values = data.get(group, {}).get("tests")
        if not isinstance(values, list) or not values or not all(isinstance(item, str) and item for item in values):
            raise GateError(f"{group}.tests must be a nonempty string list")
        if len(values) != len(set(values)):
            raise GateError(f"{group}.tests contains duplicate names")
        selected[group] = values
    total = sum(len(values) for values in selected.values())
    if total == 0 or total > maximum:
        raise GateError(f"selected test count {total} is outside enforced range 1..{maximum}")
    return maximum, selected


def find_fork_root(project_root: Path) -> Path:
    override = os.environ.get("SEEDVR2_FORK_ROOT")
    if override:
        candidate = Path(override).expanduser().resolve()
    else:
        common = Path(
            run(
                ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
                cwd=project_root,
                capture=True,
            ).strip()
        )
        checkout = common.parent if common.name == ".git" else project_root
        candidate = checkout.parent / "ComfyUI-SeedVR2_VideoUpscaler"
    if not (candidate / "tests" / "test_cli_progress.py").is_file():
        raise GateError(
            "SeedVR2 fork checkout missing; set SEEDVR2_FORK_ROOT to its repository root"
        )
    return candidate


def fork_python() -> str:
    override = os.environ.get("SEEDVR2_TEST_PYTHON")
    if override:
        return override
    runtime_python = (
        Path.home()
        / "Library/Application Support/VideoUpscaleWebUI/runtime/ComfyUI/.venv/bin/python"
    )
    if runtime_python.is_file():
        return str(runtime_python)
    return sys.executable


def validate_backend(project_root: Path, tests: list[str]) -> None:
    output = run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "tests"],
        cwd=project_root / "backend",
        capture=True,
    )
    collected = {line.strip() for line in output.splitlines() if line.startswith("tests/")}
    missing = [name for name in tests if name not in collected]
    if missing:
        raise GateError("missing backend test node(s): " + ", ".join(missing))


def validate_frontend(project_root: Path, tests: list[str]) -> tuple[Path, list[str]]:
    frontend = project_root / "frontend"
    vitest = frontend / "node_modules/.bin/vitest"
    if not vitest.is_file():
        raise GateError("frontend dependencies missing; run `cd frontend && npm ci --ignore-scripts`")
    output = run([str(vitest), "list", "--json"], cwd=frontend, capture=True)
    listed = json.loads(output)
    by_name: dict[str, Path] = {}
    duplicate_names: set[str] = set()
    for item in listed:
        name = item["name"]
        if name in by_name:
            duplicate_names.add(name)
        by_name[name] = Path(item["file"])
    ambiguous = [name for name in tests if name in duplicate_names]
    if ambiguous:
        raise GateError("ambiguous frontend test name(s): " + ", ".join(ambiguous))
    missing = [name for name in tests if name not in by_name]
    if missing:
        raise GateError("missing frontend test name(s): " + ", ".join(missing))
    files = sorted({str(by_name[name].relative_to(frontend)) for name in tests})
    return vitest, files


def validate_fork(fork_root: Path, tests: list[str]) -> None:
    parsed: dict[Path, ast.Module] = {}
    missing: list[str] = []
    for name in tests:
        parts = name.split(".")
        if len(parts) < 4 or not parts[-1].startswith("test_"):
            missing.append(name)
            continue
        path = fork_root.joinpath(*parts[:-2]).with_suffix(".py")
        tree = parsed.setdefault(path, ast.parse(path.read_text(encoding="utf-8"))) if path.is_file() else None
        found = False
        if tree is not None:
            for node in tree.body:
                if isinstance(node, ast.ClassDef) and node.name == parts[-2]:
                    found = any(
                        isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name == parts[-1]
                        for child in node.body
                    )
                    break
        if not found:
            missing.append(name)
    if missing:
        raise GateError("missing fork test name(s): " + ", ".join(missing))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="validate names and budget without running tests")
    parser.add_argument("--manifest", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parent.parent
    manifest = args.manifest or project_root / "scripts/release-tests.toml"
    maximum, selected = load_manifest(manifest)
    total = sum(len(values) for values in selected.values())
    breakdown = ", ".join(f"{group}={len(selected[group])}" for group in GROUPS)
    print(f"release gate: {total}/{maximum} tests ({breakdown})", flush=True)

    fork_root = find_fork_root(project_root)
    validate_backend(project_root, selected["backend"])
    vitest, frontend_files = validate_frontend(project_root, selected["frontend"])
    validate_fork(fork_root, selected["fork"])
    print("release gate: manifest budget and exact names valid", flush=True)
    if args.check:
        return 0

    started = time.monotonic()
    run(
        [sys.executable, "-m", "pytest", "-q", *selected["backend"]],
        cwd=project_root / "backend",
    )
    frontend_pattern = "^(?:" + "|".join(
        re.escape(name.replace(" > ", " ")) for name in selected["frontend"]
    ) + ")$"
    run(
        [str(vitest), "run", *frontend_files, "--testNamePattern", frontend_pattern],
        cwd=project_root / "frontend",
    )
    run(
        [fork_python(), "-m", "unittest", *selected["fork"], "-v"],
        cwd=fork_root,
    )
    elapsed = time.monotonic() - started
    print(f"release gate passed: {total} tests in {elapsed:.2f}s ({breakdown})", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (GateError, OSError, ValueError, json.JSONDecodeError) as error:
        print(f"release gate failed: {error}", file=sys.stderr)
        raise SystemExit(2)
