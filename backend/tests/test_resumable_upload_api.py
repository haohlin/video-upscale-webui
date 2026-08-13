import threading
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app


class ValidProbe:
    def inspect(self, path: Path) -> dict[str, float | int]:
        return {"duration_seconds": 3.5, "width": 640, "height": 360}


class RejectedProbe:
    def inspect(self, path: Path) -> dict[str, float | int]:
        raise ValueError("no video stream")


class IdleRunner:
    def preflight(self, job, limits, report_progress, is_cancelled):
        raise AssertionError("not used")

    def run(self, job, report_progress, is_cancelled):
        raise AssertionError("not used")


def client(tmp_path, *, probe=None, max_upload_bytes=16) -> TestClient:
    return TestClient(
        create_app(
            data_root=tmp_path,
            runner=IdleRunner(),
            media_probe=probe or ValidProbe(),
            max_upload_bytes=max_upload_bytes,
        ),
        headers={
            "Tailscale-User-Login": "haohan.apple@outlook.com",
            "X-Video-Upscale-Request": "1",
        },
    )


def create_session(client: TestClient, *, total_bytes=6) -> dict[str, object]:
    response = client.post(
        "/api/uploads",
        json={
            "filename": "clip.mp4",
            "total_bytes": total_bytes,
            "options": {"preset": "3b-safe", "color_correction": "lab", "output_scale": 1.0},
        },
    )
    assert response.status_code == 201
    return response.json()


def test_upload_routes_require_operator_and_mutation_header(tmp_path):
    """Removing Tailscale or same-origin checks must fail this test."""
    app = create_app(data_root=tmp_path, runner=IdleRunner(), max_upload_bytes=16)
    anonymous = TestClient(app)
    operator_without_header = TestClient(
        app, headers={"Tailscale-User-Login": "haohan.apple@outlook.com"}
    )

    assert anonymous.get("/api/uploads/missing").status_code == 403
    assert operator_without_header.post(
        "/api/uploads", json={"filename": "clip.mp4", "total_bytes": 1}
    ).status_code == 403


def test_upload_api_confirms_offsets_and_retry_does_not_duplicate_data(tmp_path):
    """Accepting a retried chunk at a stale offset must fail this test."""
    api = client(tmp_path)
    session = create_session(api)
    upload_id = session["id"]

    first = api.put(f"/api/uploads/{upload_id}", content=b"abc", headers={"Upload-Offset": "0"})
    retry = api.put(f"/api/uploads/{upload_id}", content=b"abc", headers={"Upload-Offset": "0"})
    resumed = api.get(f"/api/uploads/{upload_id}")
    second = api.put(f"/api/uploads/{upload_id}", content=b"def", headers={"Upload-Offset": "3"})

    assert first.status_code == 200
    assert first.json()["accepted_bytes"] == 3
    assert retry.status_code == 409
    assert retry.json()["detail"] == "Upload offset does not match accepted bytes"
    assert resumed.json()["accepted_bytes"] == 3
    assert second.json()["accepted_bytes"] == 6


def test_upload_api_resumes_after_application_restart(tmp_path):
    """Keeping API offset only in an application instance must fail this test."""
    first_api = client(tmp_path)
    upload_id = create_session(first_api)["id"]
    assert first_api.put(
        f"/api/uploads/{upload_id}", content=b"abc", headers={"Upload-Offset": "0"}
    ).status_code == 200

    restarted_api = client(tmp_path)
    status = restarted_api.get(f"/api/uploads/{upload_id}")

    assert status.status_code == 200
    assert status.json()["accepted_bytes"] == 3


def test_upload_chunk_has_four_mebibyte_raw_body_limit(tmp_path):
    """Letting one resumable request exceed four MiB must fail this test."""
    api = client(tmp_path, max_upload_bytes=5 * 1024 * 1024)
    upload_id = create_session(api, total_bytes=5 * 1024 * 1024)["id"]

    response = api.put(
        f"/api/uploads/{upload_id}",
        content=b"x" * (4 * 1024 * 1024 + 1),
        headers={"Upload-Offset": "0"},
    )

    assert response.status_code == 413
    assert api.get(f"/api/uploads/{upload_id}").json()["accepted_bytes"] == 0


def test_delete_discards_incomplete_upload_session(tmp_path):
    """Leaving an explicitly discarded session resumable must fail this test."""
    api = client(tmp_path)
    upload_id = create_session(api)["id"]

    deleted = api.delete(f"/api/uploads/{upload_id}")

    assert deleted.status_code == 204
    assert api.get(f"/api/uploads/{upload_id}").status_code == 404


def test_finalize_returns_normal_job_and_discards_accepted_session(tmp_path):
    """Leaving a finalized staged upload accessible must fail this test."""
    api = client(tmp_path)
    upload_id = create_session(api)["id"]
    assert api.put(
        f"/api/uploads/{upload_id}", content=b"abcdef", headers={"Upload-Offset": "0"}
    ).status_code == 200

    finalized = api.post(f"/api/uploads/{upload_id}/finalize")

    assert finalized.status_code == 201
    assert finalized.json()["original_filename"] == "clip.mp4"
    assert api.get(f"/api/uploads/{upload_id}").status_code == 404


def test_finalize_rejection_discards_invalid_session_but_transient_error_keeps_it(tmp_path, monkeypatch):
    """Keeping rejected input or deleting retryable input must fail this test."""
    rejected_api = client(tmp_path / "rejected", probe=RejectedProbe())
    rejected_id = create_session(rejected_api)["id"]
    rejected_api.put(f"/api/uploads/{rejected_id}", content=b"abcdef", headers={"Upload-Offset": "0"})
    assert rejected_api.post(f"/api/uploads/{rejected_id}/finalize").status_code == 422
    assert rejected_api.get(f"/api/uploads/{rejected_id}").status_code == 404

    transient_api = client(tmp_path / "transient")
    transient_id = create_session(transient_api)["id"]
    transient_api.put(f"/api/uploads/{transient_id}", content=b"abcdef", headers={"Upload-Offset": "0"})
    monkeypatch.setattr(
        transient_api.app.state.job_service.media_probe,
        "inspect",
        lambda path: (_ for _ in ()).throw(OSError("temporary probe failure")),
    )
    assert transient_api.post(f"/api/uploads/{transient_id}/finalize").status_code == 503
    assert transient_api.get(f"/api/uploads/{transient_id}").status_code == 200


def test_concurrent_finalize_claims_one_session_for_one_job(tmp_path):
    """Allowing two finalizers to create two jobs must fail this test."""
    entered = threading.Event()
    release = threading.Event()

    class BlockingProbe(ValidProbe):
        def inspect(self, path):
            entered.set()
            assert release.wait(timeout=1)
            return super().inspect(path)

    app = create_app(
        data_root=tmp_path,
        runner=IdleRunner(),
        media_probe=BlockingProbe(),
        max_upload_bytes=16,
    )
    first = TestClient(app, headers={"Tailscale-User-Login": "haohan.apple@outlook.com", "X-Video-Upscale-Request": "1"})
    second = TestClient(app, headers={"Tailscale-User-Login": "haohan.apple@outlook.com", "X-Video-Upscale-Request": "1"})
    upload_id = create_session(first)["id"]
    first.put(f"/api/uploads/{upload_id}", content=b"abcdef", headers={"Upload-Offset": "0"})
    responses = []

    thread = threading.Thread(target=lambda: responses.append(first.post(f"/api/uploads/{upload_id}/finalize")))
    thread.start()
    assert entered.wait(timeout=1)
    second_thread = threading.Thread(
        target=lambda: responses.append(second.post(f"/api/uploads/{upload_id}/finalize"))
    )
    second_thread.start()
    release.set()
    thread.join(timeout=1)
    second_thread.join(timeout=1)

    assert not thread.is_alive()
    assert not second_thread.is_alive()
    assert sorted(response.status_code for response in responses) == [201, 409]
    assert len(first.get("/api/jobs").json()["jobs"]) == 1


def test_transient_finalization_store_failure_removes_input_link(tmp_path, monkeypatch):
    """Leaving hard-linked input after unaccepted job must fail this test."""
    api = client(tmp_path)
    upload_id = create_session(api)["id"]
    api.put(f"/api/uploads/{upload_id}", content=b"abcdef", headers={"Upload-Offset": "0"})
    monkeypatch.setattr(
        api.app.state.job_service.store,
        "create",
        lambda **kwargs: (_ for _ in ()).throw(OSError("database temporarily unavailable")),
    )

    response = api.post(f"/api/uploads/{upload_id}/finalize")

    assert response.status_code == 503
    assert list((tmp_path / "inputs").iterdir()) == []
    assert api.get(f"/api/uploads/{upload_id}").status_code == 200


def test_create_upload_metadata_body_is_bounded_before_json_parsing(tmp_path):
    """Parsing more than 64 KiB of session JSON must fail this test."""
    api = client(tmp_path)

    response = api.post(
        "/api/uploads",
        content=b"{" + b" " * (64 * 1024),
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 413


def test_restart_recovers_claim_and_returns_durably_created_job_idempotently(tmp_path, monkeypatch):
    """Keeping crash claims or recreating an accepted session job must fail this test."""
    claimed_api = client(tmp_path / "claimed")
    claimed_id = create_session(claimed_api)["id"]
    claimed_api.put(f"/api/uploads/{claimed_id}", content=b"abcdef", headers={"Upload-Offset": "0"})
    claimed_api.app.state.upload_session_service.claim_finalization(claimed_id)

    after_claim_restart = client(tmp_path / "claimed")
    recovered = after_claim_restart.post(f"/api/uploads/{claimed_id}/finalize")

    assert recovered.status_code == 201
    assert recovered.json()["id"] == claimed_id

    accepted_api = client(tmp_path / "accepted")
    accepted_id = create_session(accepted_api)["id"]
    accepted_api.put(f"/api/uploads/{accepted_id}", content=b"abcdef", headers={"Upload-Offset": "0"})
    original_cleanup = accepted_api.app.state.upload_session_service.complete_finalization
    monkeypatch.setattr(
        accepted_api.app.state.upload_session_service,
        "complete_finalization",
        lambda upload_id: (_ for _ in ()).throw(OSError("crash before session cleanup")),
    )
    crashing = TestClient(
        accepted_api.app,
        headers={"Tailscale-User-Login": "haohan.apple@outlook.com", "X-Video-Upscale-Request": "1"},
        raise_server_exceptions=False,
    )
    assert crashing.post(f"/api/uploads/{accepted_id}/finalize").status_code == 500
    monkeypatch.setattr(
        accepted_api.app.state.upload_session_service,
        "complete_finalization",
        original_cleanup,
    )

    after_accept_restart = client(tmp_path / "accepted")
    repeated = after_accept_restart.post(f"/api/uploads/{accepted_id}/finalize")

    assert repeated.status_code == 201
    assert repeated.json()["id"] == accepted_id
    assert len(after_accept_restart.get("/api/jobs").json()["jobs"]) == 1
    assert [path.stem for path in (tmp_path / "accepted" / "inputs").iterdir()] == [accepted_id]
    assert after_accept_restart.get(f"/api/uploads/{accepted_id}").status_code == 404
