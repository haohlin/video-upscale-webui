#!/usr/bin/env python3
"""Refuse runtime updates while jobs are active and quiesce one LaunchAgent."""

from __future__ import annotations

import argparse
import sqlite3
import subprocess
from pathlib import Path


BLOCKING_STATUSES = ("queued", "preflight", "running")


def blocking_count(connection: sqlite3.Connection) -> int:
    return int(
        connection.execute(
            "SELECT COUNT(*) FROM jobs WHERE status IN (?, ?, ?)",
            BLOCKING_STATUSES,
        ).fetchone()[0]
    )


def _stop_if_loaded(launchctl: str, domain: str) -> None:
    loaded = (
        subprocess.run(
            [launchctl, "print", domain],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode
        == 0
    )
    if loaded:
        subprocess.run([launchctl, "bootout", domain], check=True)


def quiesce(database: Path, launchctl: str, domain: str, check_only: bool) -> int:
    if not database.exists():
        if not check_only:
            _stop_if_loaded(launchctl, domain)
        return 0
    with sqlite3.connect(database, timeout=30) as connection:
        connection.execute("BEGIN IMMEDIATE")
        if blocking_count(connection):
            return 75
        if not check_only:
            _stop_if_loaded(launchctl, domain)
            if blocking_count(connection):
                return 76
        connection.commit()
    return 0


def parser() -> argparse.ArgumentParser:
    argument_parser = argparse.ArgumentParser()
    argument_parser.add_argument("--database", required=True, type=Path)
    argument_parser.add_argument("--launchctl", required=True)
    argument_parser.add_argument("--domain", required=True)
    argument_parser.add_argument("--check-only", action="store_true")
    return argument_parser


def main() -> int:
    arguments = parser().parse_args()
    return quiesce(
        arguments.database,
        arguments.launchctl,
        arguments.domain,
        arguments.check_only,
    )


if __name__ == "__main__":
    raise SystemExit(main())
