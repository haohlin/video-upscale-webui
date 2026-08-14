from app.system_metrics import SystemMetrics


def test_cuda_metrics_uses_existing_wsl_nvidia_smi_when_service_path_omits_it(monkeypatch):
    collector = SystemMetrics("nvidia-cuda")
    calls: list[list[str]] = []

    def capture(arguments: list[str]) -> str:
        calls.append(arguments)
        return "91, 12288, 24564\n"

    monkeypatch.setattr("app.system_metrics.shutil.which", lambda _name: None)
    monkeypatch.setattr(collector, "_run", capture)

    utilization, used, total = collector._gpu(None)

    assert calls[0][0] == "/usr/lib/wsl/lib/nvidia-smi"
    assert utilization == 91.0
    assert used == 12288 * 1024**2
    assert total == 24564 * 1024**2
