import base64

from fastapi.testclient import TestClient

from app.main import create_app
from app.runner import UnavailableRunner


class ReadyRunner:
    def preflight(self, job, limits, report_progress, is_cancelled):
        raise AssertionError("not used")

    def run(self, job, report_progress, is_cancelled):
        raise AssertionError("not used")


def auth_headers() -> dict[str, str]:
    credentials = base64.b64encode(b"video:test-access-token").decode("ascii")
    return {"Authorization": f"Basic {credentials}"}


def test_health_reports_ready_status_for_available_runner(tmp_path):
    """Reporting a ready runner as degraded must make this test fail."""
    client = TestClient(create_app(data_root=tmp_path, runner=ReadyRunner()))

    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "runner": "ready"}


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
    }


def test_every_response_denies_cross_origin_framing(tmp_path):
    """Missing anti-framing headers would permit clickjacking operator controls."""
    client = TestClient(create_app(data_root=tmp_path, runner=ReadyRunner()))

    response = client.get("/api/jobs", headers=auth_headers())

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

    root = client.get("/", headers=auth_headers())
    health = client.get("/api/health")

    assert root.status_code == 200
    assert "Video Upscale WebUI" in root.text
    assert health.json() == {"status": "ok", "runner": "ready"}


def test_private_routes_require_runtime_access_token(tmp_path):
    """Serving private media routes without authentication must make this test fail."""
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

    root = client.get("/")
    jobs = client.get("/api/jobs")
    health = client.get("/api/health")

    assert root.status_code == 401
    assert jobs.status_code == 401
    assert root.headers["www-authenticate"] == 'Basic realm="Video Upscale"'
    assert health.status_code == 200


def test_authenticated_private_route_accepts_constant_runtime_token(tmp_path):
    """Rejecting the configured operator credential must make this test fail."""
    client = TestClient(create_app(data_root=tmp_path, runner=ReadyRunner()))

    response = client.get("/api/jobs", headers=auth_headers())

    assert response.status_code == 200
    assert response.json() == {"jobs": []}


def test_state_change_requires_same_origin_request_header(tmp_path):
    """Allowing a cross-origin simple POST with cached Basic credentials must fail."""
    client = TestClient(create_app(data_root=tmp_path, runner=ReadyRunner()))
    headers = auth_headers()

    response = client.post("/api/jobs/missing/cancel", headers=headers)

    assert response.status_code == 403
