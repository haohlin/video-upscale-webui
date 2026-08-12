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
    assert health.json() == {"status": "ok", "runner": "ready"}


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
