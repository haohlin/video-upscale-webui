from fastapi.testclient import TestClient
import pytest

from app.main import create_app
from app.runner import UnavailableRunner


class ReadyRunner:
    def preflight(self, job, limits, report_progress, is_cancelled):
        raise AssertionError("not used")

    def run(self, job, report_progress, is_cancelled):
        raise AssertionError("not used")


def operator_headers() -> dict[str, str]:
    return {"Tailscale-User-Login": "\tHAOHAN.APPLE@OUTLOOK.COM "}


def test_health_reports_ready_status_for_available_runner(tmp_path):
    """Reporting a ready runner as degraded must make this test fail."""
    client = TestClient(create_app(data_root=tmp_path, runner=ReadyRunner()))

    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "runner": "ready",
        "backend_id": "mac",
        "display_name": "Mac M4 Pro",
        "platform": "macos",
        "accelerator": "Apple MPS",
        "state": "ready",
        "presets": ["3b-safe", "7b-fp8-experimental"],
    }


def test_health_reports_degraded_when_seedvr2_runner_is_unavailable(tmp_path):
    """Returning OK for a missing SeedVR2 adapter must make this test fail."""
    client = TestClient(
        create_app(data_root=tmp_path, runner=UnavailableRunner("SeedVR2 adapter is missing"))
    )

    response = client.get("/api/health")

    assert response.status_code == 503
    assert response.json() == {
        "status": "degraded",
        "runner": "unavailable",
        "backend_id": "mac",
        "display_name": "Mac M4 Pro",
        "platform": "macos",
        "accelerator": "Apple MPS",
        "state": "offline",
        "presets": ["3b-safe", "7b-fp8-experimental"],
    }


def test_every_response_denies_cross_origin_framing(tmp_path):
    """Missing anti-framing headers would permit clickjacking operator controls."""
    client = TestClient(create_app(data_root=tmp_path, runner=ReadyRunner()))

    response = client.get("/api/jobs", headers=operator_headers())

    assert response.headers["content-security-policy"] == "frame-ancestors 'none'"
    assert response.headers["x-frame-options"] == "DENY"


def test_built_frontend_is_served_at_root_without_capturing_api_routes(tmp_path):
    """Replacing built WebUI with API JSON at root must make this test fail."""
    frontend_dist = tmp_path / "frontend-dist"
    frontend_dist.mkdir()
    (frontend_dist / "index.html").write_text("<main>Video Upscale WebUI</main>")
    client = TestClient(
        create_app(
            data_root=tmp_path / "data",
            runner=ReadyRunner(),
            frontend_dist=frontend_dist,
        )
    )

    root = client.get("/", headers=operator_headers())
    health = client.get("/api/health")

    assert root.status_code == 200
    assert "Video Upscale WebUI" in root.text
    assert health.json()["status"] == "ok"
    assert health.json()["backend_id"] == "mac"


@pytest.mark.parametrize(
    "identity",
    [
        None,
        "intruder@example.com",
        "tag:video-upscale",
        b"\xa0haohan.apple@outlook.com\xa0",
    ],
    ids=["missing", "wrong-user", "tagged-node", "non-ascii"],
)
def test_private_routes_reject_untrusted_tailscale_identity_without_challenge(
    tmp_path, identity
):
    """Accepting absent, wrong, tagged, or malformed identity must fail."""
    frontend_dist = tmp_path / "frontend-dist"
    frontend_dist.mkdir()
    (frontend_dist / "index.html").write_text("<main>private</main>")
    client = TestClient(
        create_app(
            data_root=tmp_path / "data",
            runner=ReadyRunner(),
            frontend_dist=frontend_dist,
        )
    )

    headers = {} if identity is None else {"Tailscale-User-Login": identity}
    root = client.get("/", headers=headers)
    jobs = client.get("/api/jobs", headers=headers)
    health = client.get("/api/health")

    assert root.status_code == 403
    assert jobs.status_code == 403
    assert "www-authenticate" not in root.headers
    assert "www-authenticate" not in jobs.headers
    assert health.status_code == 200


def test_private_route_accepts_case_insensitive_ascii_trimmed_operator_identity(tmp_path):
    """Rejecting configured Tailscale operator after ASCII trim/case-fold must fail."""
    client = TestClient(create_app(data_root=tmp_path, runner=ReadyRunner()))

    response = client.get("/api/jobs", headers=operator_headers())

    assert response.status_code == 200
    assert response.json() == {"jobs": []}


def test_state_change_requires_same_origin_request_header(tmp_path):
    """Allowing operator mutation without custom request header must fail."""
    client = TestClient(create_app(data_root=tmp_path, runner=ReadyRunner()))
    headers = operator_headers()

    response = client.post("/api/jobs/missing/cancel", headers=headers)

    assert response.status_code == 403


def test_cuda_backend_reports_4090_capabilities_and_presets(tmp_path, monkeypatch):
    monkeypatch.setenv("VIDEO_UPSCALE_BACKEND_ID", "windows-4090")
    monkeypatch.setenv("VIDEO_UPSCALE_BACKEND_DISPLAY_NAME", "Windows RTX 4090")
    monkeypatch.setenv("VIDEO_UPSCALE_PLATFORM_NAME", "wsl2")
    monkeypatch.setenv("VIDEO_UPSCALE_ACCELERATOR_NAME", "NVIDIA GeForce RTX 4090")
    monkeypatch.setenv("VIDEO_UPSCALE_DEVICE_BACKEND_CLASS", "nvidia-cuda")
    monkeypatch.setenv("VIDEO_UPSCALE_DEFAULT_PROFILE", "7b-fp8-quality")
    client = TestClient(create_app(data_root=tmp_path, runner=ReadyRunner()))

    health = client.get("/api/health").json()
    config = client.get("/api/config", headers=operator_headers()).json()

    assert health == {
        "status": "ok",
        "runner": "ready",
        "backend_id": "windows-4090",
        "display_name": "Windows RTX 4090",
        "platform": "wsl2",
        "accelerator": "NVIDIA GeForce RTX 4090",
        "state": "ready",
        "presets": ["7b-fp8-quality", "3b-fp8-fast"],
    }
    assert config["default_profile"] == "7b-fp8-quality"
    assert config["presets"] == ["7b-fp8-quality", "3b-fp8-fast"]


def test_windows_backend_allows_only_configured_mac_web_origin(tmp_path, monkeypatch):
    monkeypatch.setenv("VIDEO_UPSCALE_ALLOWED_WEB_ORIGIN", "https://mac.tailnet.ts.net:8444")
    client = TestClient(create_app(data_root=tmp_path, runner=ReadyRunner()))

    allowed = client.options(
        "/api/jobs",
        headers={
            "Origin": "https://mac.tailnet.ts.net:8444",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "X-Video-Upscale-Request",
        },
    )
    rejected = client.options(
        "/api/jobs",
        headers={
            "Origin": "https://attacker.example",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == "https://mac.tailnet.ts.net:8444"
    assert rejected.status_code == 400
    assert "access-control-allow-origin" not in rejected.headers
