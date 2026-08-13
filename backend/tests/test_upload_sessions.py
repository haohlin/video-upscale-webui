import json
import re
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

import app.upload_sessions as upload_sessions
from app.config import Settings
from app.upload_sessions import UploadSessionError, UploadSessionService


def make_settings(tmp_path, *, max_upload_bytes=32):
    return replace(
        Settings.from_environment().with_data_root(tmp_path, max_upload_bytes),
        disk_reserve_gb=0,
        upload_session_ttl_seconds=60,
    )


def test_create_returns_opaque_id_and_public_progress_fields(tmp_path):
    """Replacing random opaque IDs with predictable names must fail this test."""
    service = UploadSessionService(make_settings(tmp_path))

    session = service.create(filename="holiday.mp4", total_bytes=12)

    assert re.fullmatch(r"[A-Za-z0-9_-]{32,}", session["id"])
    assert session["filename"] == "holiday.mp4"
    assert session["total_bytes"] == 12
    assert session["accepted_bytes"] == 0
    assert datetime.fromisoformat(session["expires_at"]).tzinfo is not None


def test_append_accepts_only_expected_offset_and_preserves_existing_bytes(tmp_path):
    """Accepting an out-of-order chunk must fail this test."""
    service = UploadSessionService(make_settings(tmp_path))
    session = service.create(filename="clip.mp4", total_bytes=6)

    accepted = service.append(session["id"], offset=0, data=b"abc")
    assert accepted["accepted_bytes"] == 3

    with pytest.raises(UploadSessionError, match="Upload offset does not match") as error:
        service.append(session["id"], offset=0, data=b"def")

    assert error.value.status_code == 409
    assert service.status(session["id"])["accepted_bytes"] == 3
    assert service.append(session["id"], offset=3, data=b"def")["accepted_bytes"] == 6


def test_status_recovers_persisted_offset_after_service_restart(tmp_path):
    """Keeping accepted bytes only in memory must fail this test."""
    settings = make_settings(tmp_path)
    first = UploadSessionService(settings)
    session = first.create(filename="resume.mov", total_bytes=7)
    first.append(session["id"], offset=0, data=b"resume-")

    recovered = UploadSessionService(settings).status(session["id"])

    assert recovered["id"] == session["id"]
    assert recovered["accepted_bytes"] == 7
    assert recovered["total_bytes"] == 7


def test_restart_recovers_durable_chunk_when_metadata_update_fails(tmp_path, monkeypatch):
    """Leaving a fsynced chunk unconfirmed after metadata failure must fail this test."""
    settings = make_settings(tmp_path)
    first = UploadSessionService(settings)
    session = first.create(filename="resume.mov", total_bytes=6)
    original_write = first._write_metadata

    def fail_appended_metadata(session_id, record):
        if record["accepted_bytes"] == 3:
            raise OSError("metadata storage unavailable")
        original_write(session_id, record)

    monkeypatch.setattr(first, "_write_metadata", fail_appended_metadata)
    with pytest.raises(OSError, match="metadata storage unavailable"):
        first.append(session["id"], offset=0, data=b"abc")

    restarted = UploadSessionService(settings)
    assert restarted.status(session["id"])["accepted_bytes"] == 3
    assert restarted.append(session["id"], offset=3, data=b"def")["accepted_bytes"] == 6


def test_discard_waits_for_paused_append_before_removing_session_files(tmp_path, monkeypatch):
    """Removing files during a successful append must fail this test."""
    service = UploadSessionService(make_settings(tmp_path))
    session = service.create(filename="clip.mp4", total_bytes=3)
    append_ready = threading.Event()
    release_append = threading.Event()
    removal_started = threading.Event()
    append_errors = []
    discard_errors = []
    original_write = service._write_metadata
    original_remove = service._remove_files

    def pause_appended_metadata(session_id, record):
        if record["accepted_bytes"] == 3:
            append_ready.set()
            assert release_append.wait(timeout=1)
        original_write(session_id, record)

    def observe_removal(session_id):
        removal_started.set()
        original_remove(session_id)

    monkeypatch.setattr(service, "_write_metadata", pause_appended_metadata)
    monkeypatch.setattr(service, "_remove_files", observe_removal)

    def append():
        try:
            service.append(session["id"], offset=0, data=b"all")
        except Exception as error:  # pragma: no cover - asserted below
            append_errors.append(error)

    def discard():
        try:
            service.discard(session["id"])
        except Exception as error:  # pragma: no cover - asserted below
            discard_errors.append(error)

    append_thread = threading.Thread(target=append)
    append_thread.start()
    assert append_ready.wait(timeout=1)
    discard_thread = threading.Thread(target=discard)
    discard_thread.start()
    removed_while_append_was_paused = removal_started.wait(timeout=0.1)
    release_append.set()
    append_thread.join(timeout=1)
    discard_thread.join(timeout=1)

    assert not append_thread.is_alive()
    assert not discard_thread.is_alive()
    assert append_errors == []
    assert discard_errors == []
    assert not removed_while_append_was_paused
    with pytest.raises(UploadSessionError) as error:
        service.status(session["id"])
    assert error.value.status_code == 404


def test_expiry_cleanup_waits_for_paused_append_before_removing_files(tmp_path, monkeypatch):
    """Expiry cleanup removing files during a successful append must fail this test."""
    now = datetime(2026, 8, 13, tzinfo=UTC)
    current = now
    settings = make_settings(tmp_path)
    service = UploadSessionService(settings, now=lambda: current)
    session = service.create(filename="clip.mp4", total_bytes=3)
    append_ready = threading.Event()
    release_append = threading.Event()
    removal_started = threading.Event()
    append_errors = []
    original_write = service._write_metadata
    original_remove = service._remove_files

    def pause_appended_metadata(session_id, record):
        if record["accepted_bytes"] == 3:
            append_ready.set()
            assert release_append.wait(timeout=1)
        original_write(session_id, record)

    def observe_removal(session_id):
        removal_started.set()
        original_remove(session_id)

    monkeypatch.setattr(service, "_write_metadata", pause_appended_metadata)
    monkeypatch.setattr(service, "_remove_files", observe_removal)

    def append():
        try:
            service.append(session["id"], offset=0, data=b"all")
        except Exception as error:  # pragma: no cover - asserted below
            append_errors.append(error)

    append_thread = threading.Thread(target=append)
    append_thread.start()
    assert append_ready.wait(timeout=1)
    current += timedelta(seconds=settings.upload_session_ttl_seconds + 1)
    cleanup_thread = threading.Thread(target=service._cleanup_expired)
    cleanup_thread.start()
    removed_while_append_was_paused = removal_started.wait(timeout=0.1)
    release_append.set()
    append_thread.join(timeout=1)
    cleanup_thread.join(timeout=1)

    assert not append_thread.is_alive()
    assert not cleanup_thread.is_alive()
    assert append_errors == []
    assert not removed_while_append_was_paused
    assert list((tmp_path / "staging").iterdir()) == []


@pytest.mark.parametrize("total_bytes", [0, -1, 33])
def test_create_rejects_total_size_outside_configured_bounds(tmp_path, total_bytes):
    """Allowing empty, negative, or oversized sessions must fail this test."""
    service = UploadSessionService(make_settings(tmp_path))

    with pytest.raises(UploadSessionError) as error:
        service.create(filename="clip.mp4", total_bytes=total_bytes)

    assert error.value.status_code == 413


def test_expired_session_is_unavailable_and_removes_its_staging_files(tmp_path):
    """Retaining an expired upload or its staging data must fail this test."""
    now = datetime(2026, 8, 13, tzinfo=UTC)
    current = now
    settings = make_settings(tmp_path)
    service = UploadSessionService(settings, now=lambda: current)
    session = service.create(filename="old.mp4", total_bytes=3)
    service.append(session["id"], offset=0, data=b"old")

    current += timedelta(seconds=settings.upload_session_ttl_seconds + 1)

    with pytest.raises(UploadSessionError, match="Upload session has expired") as error:
        service.status(session["id"])

    assert error.value.status_code == 410
    assert list((tmp_path / "staging").iterdir()) == []


def test_append_does_not_follow_replaced_staging_data_symlink(tmp_path):
    """Following a staging symlink must fail this test."""
    service = UploadSessionService(make_settings(tmp_path))
    session = service.create(filename="clip.mp4", total_bytes=3)
    staging = tmp_path / "staging"
    data_path = staging / f"{session['id']}.part"
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"safe")
    data_path.unlink()
    data_path.symlink_to(outside)

    with pytest.raises(UploadSessionError, match="Unsafe upload staging file") as error:
        service.append(session["id"], offset=0, data=b"bad")

    assert error.value.status_code == 409
    assert outside.read_bytes() == b"safe"


def test_corrupt_session_metadata_fails_closed(tmp_path):
    """Returning malformed persisted state to callers must fail this test."""
    service = UploadSessionService(make_settings(tmp_path))
    session = service.create(filename="clip.mp4", total_bytes=3)
    metadata = tmp_path / "staging" / f"{session['id']}.json"
    metadata.write_text("not-json", encoding="utf-8")

    with pytest.raises(UploadSessionError) as error:
        service.status(session["id"])

    assert error.value.status_code == 409


def test_metadata_write_retries_short_os_writes(tmp_path, monkeypatch):
    """Publishing only first partial metadata write must fail this test."""
    service = UploadSessionService(make_settings(tmp_path))
    original_write = upload_sessions.os.write
    shortened = False

    def write_metadata_in_two_parts(fd, data):
        nonlocal shortened
        if not shortened and data.startswith(b"{"):
            shortened = True
            return original_write(fd, data[: len(data) // 2])
        return original_write(fd, data)

    monkeypatch.setattr(upload_sessions.os, "write", write_metadata_in_two_parts)
    session = service.create(filename="clip.mp4", total_bytes=3)

    assert service.status(session["id"])["accepted_bytes"] == 0


def test_invalid_persisted_options_fail_closed_before_public_status(tmp_path):
    """Accepting malformed persisted options must fail this test."""
    service = UploadSessionService(make_settings(tmp_path))
    session = service.create(filename="clip.mp4", total_bytes=3)
    metadata = tmp_path / "staging" / f"{session['id']}.json"
    record = json.loads(metadata.read_text(encoding="utf-8"))
    record["options"] = "not-an-object"
    metadata.write_text(json.dumps(record), encoding="utf-8")

    with pytest.raises(UploadSessionError) as error:
        service.status(session["id"])

    assert error.value.status_code == 409


def test_create_rejects_options_that_exceed_metadata_size_limit(tmp_path):
    """Allowing unbounded persisted options must fail this test."""
    service = UploadSessionService(make_settings(tmp_path))

    with pytest.raises(UploadSessionError, match="Upload options are too large") as error:
        service.create(filename="clip.mp4", total_bytes=3, options={"note": "x" * 70_000})

    assert error.value.status_code == 413


def test_finalize_requires_complete_data_and_discard_removes_session(tmp_path):
    """Finalizing partial data or retaining discarded files must fail this test."""
    service = UploadSessionService(make_settings(tmp_path))
    session = service.create(filename="clip.mp4", total_bytes=3)

    with pytest.raises(UploadSessionError, match="Upload is incomplete") as error:
        service.finalize(session["id"])
    assert error.value.status_code == 409

    service.append(session["id"], offset=0, data=b"all")
    completed = service.finalize(session["id"])
    assert completed.path.read_bytes() == b"all"
    service.discard(session["id"])
    assert list((tmp_path / "staging").iterdir()) == []
