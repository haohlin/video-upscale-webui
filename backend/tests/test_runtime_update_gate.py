import importlib.util
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def load_gate():
    path = PROJECT_ROOT / "scripts" / "runtime-update-gate.py"
    spec = importlib.util.spec_from_file_location("runtime_update_gate", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def create_jobs_database(path: Path, status: str | None = None) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, status TEXT NOT NULL)")
        if status is not None:
            connection.execute(
                "INSERT INTO jobs (id, status) VALUES (?, ?)",
                ("job-1", status),
            )


@pytest.mark.parametrize("status", ["queued", "preflight", "running"])
def test_blocking_job_prevents_launchagent_stop_and_preserves_files(tmp_path, status):
    gate = load_gate()
    database = tmp_path / "jobs.sqlite3"
    marker = tmp_path / "runtime-marker"
    launchctl_log = tmp_path / "launchctl.log"
    launchctl = tmp_path / "launchctl"
    create_jobs_database(database, status)
    marker.write_text("unchanged")
    launchctl.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> {launchctl_log}\n")
    launchctl.chmod(0o755)
    before_database = database.read_bytes()

    result = gate.quiesce(
        database,
        str(launchctl),
        "gui/501/com.haohanl.video-upscale-webui",
        check_only=False,
    )

    assert result == 75
    assert not launchctl_log.exists()
    assert database.read_bytes() == before_database
    assert marker.read_text() == "unchanged"


def test_empty_queue_stops_exact_launchagent_while_write_lock_is_held(tmp_path):
    gate = load_gate()
    database = tmp_path / "jobs.sqlite3"
    launchctl_log = tmp_path / "launchctl.log"
    lock_log = tmp_path / "lock.log"
    launchctl = tmp_path / "launchctl"
    create_jobs_database(database)
    launchctl.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$*\" >> {launchctl_log}\n"
        "if [ \"$1\" = bootout ]; then\n"
        f"  python3 - {str(database)!r} <<'PY'\n"
        "import sqlite3\n"
        "import sys\n"
        "try:\n"
        "    with sqlite3.connect(sys.argv[1], timeout=0) as connection:\n"
        "        connection.execute(\"INSERT INTO jobs (id, status) VALUES ('late', 'queued')\")\n"
        "except sqlite3.OperationalError as error:\n"
        f"    open({str(lock_log)!r}, 'w').write(str(error))\n"
        "PY\n"
        "fi\n"
        "exit 0\n"
    )
    launchctl.chmod(0o755)

    result = gate.quiesce(
        database,
        str(launchctl),
        "gui/501/com.haohanl.video-upscale-webui",
        check_only=False,
    )

    assert result == 0
    assert launchctl_log.read_text().splitlines() == [
        "print gui/501/com.haohanl.video-upscale-webui",
        "bootout gui/501/com.haohanl.video-upscale-webui",
    ]
    assert "database is locked" in lock_log.read_text()


def test_job_detected_after_stop_returns_76_before_mutation(tmp_path):
    gate = load_gate()
    database = tmp_path / "jobs.sqlite3"
    launchctl = tmp_path / "launchctl"
    create_jobs_database(database)
    launchctl.write_text("#!/bin/sh\nexit 0\n")
    launchctl.chmod(0o755)

    with patch.object(gate, "blocking_count", side_effect=[0, 1]):
        result = gate.quiesce(
            database,
            str(launchctl),
            "gui/501/com.haohanl.video-upscale-webui",
            check_only=False,
        )

    assert result == 76
