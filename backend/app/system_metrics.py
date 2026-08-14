from __future__ import annotations

import ctypes
from datetime import datetime, timezone
import os
import plistlib
from pathlib import Path
import re
import subprocess
import sys
from threading import Lock


class SystemMetrics:
    """Small, best-effort host sampler for the two supported runtimes."""

    def __init__(self, device_backend_class: str):
        self.device_backend_class = device_backend_class
        self._lock = Lock()
        self._last_cpu: tuple[int, int] | None = None

    def snapshot(self) -> dict[str, float | int | str | None]:
        with self._lock:
            cpu_percent = self._cpu_percent()
            ram_used, ram_total = self._memory()
            gpu_percent, gpu_used, gpu_total = self._gpu(ram_total)
        return {
            "sampled_at": datetime.now(timezone.utc).isoformat(),
            "cpu_percent": cpu_percent,
            "ram_used_bytes": ram_used,
            "ram_total_bytes": ram_total,
            "gpu_percent": gpu_percent,
            "gpu_memory_used_bytes": gpu_used,
            "gpu_memory_total_bytes": gpu_total,
        }

    def _cpu_percent(self) -> float | None:
        try:
            current = self._linux_cpu() if sys.platform.startswith("linux") else self._darwin_cpu()
            total, idle = current
            previous = self._last_cpu
            self._last_cpu = current
            if previous is None:
                active = total - idle
                return round(100 * active / total, 1) if total else None
            total_delta = total - previous[0]
            idle_delta = idle - previous[1]
            return round(100 * (total_delta - idle_delta) / total_delta, 1) if total_delta > 0 else None
        except (OSError, ValueError, AttributeError):
            return None

    @staticmethod
    def _linux_cpu() -> tuple[int, int]:
        fields = Path("/proc/stat").read_text().splitlines()[0].split()[1:]
        ticks = [int(field) for field in fields]
        return sum(ticks), ticks[3] + (ticks[4] if len(ticks) > 4 else 0)

    @staticmethod
    def _darwin_cpu() -> tuple[int, int]:
        library = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
        library.mach_host_self.restype = ctypes.c_uint
        ticks = (ctypes.c_uint32 * 4)()
        count = ctypes.c_uint(4)
        result = library.host_statistics(
            library.mach_host_self(), 3, ctypes.byref(ticks), ctypes.byref(count)
        )
        if result != 0:
            raise OSError("host_statistics failed")
        values = [int(value) for value in ticks]
        return sum(values), values[2]

    def _memory(self) -> tuple[int | None, int | None]:
        try:
            if sys.platform.startswith("linux"):
                values: dict[str, int] = {}
                for line in Path("/proc/meminfo").read_text().splitlines():
                    key, raw = line.split(":", 1)
                    if key in {"MemTotal", "MemAvailable"}:
                        values[key] = int(raw.split()[0]) * 1024
                total = values["MemTotal"]
                return total - values["MemAvailable"], total
            page_size = os.sysconf("SC_PAGE_SIZE")
            total = os.sysconf("SC_PHYS_PAGES") * page_size
            output = self._run(["vm_stat"])
            pages = {
                name: int(value.replace(".", ""))
                for name, value in re.findall(r"^Pages (free|inactive|speculative):\s+(\d+\.)$", output, re.MULTILINE)
            }
            available = sum(pages.values()) * page_size
            return max(0, total - available), total
        except (OSError, ValueError, KeyError):
            return None, None

    def _gpu(self, ram_total: int | None) -> tuple[float | None, int | None, int | None]:
        try:
            if self.device_backend_class == "nvidia-cuda":
                output = self._run([
                    "nvidia-smi",
                    "--query-gpu=utilization.gpu,memory.used,memory.total",
                    "--format=csv,noheader,nounits",
                ])
                utilization, used_mib, total_mib = [float(value.strip()) for value in output.splitlines()[0].split(",")]
                return round(utilization, 1), int(used_mib * 1024**2), int(total_mib * 1024**2)
            if sys.platform == "darwin":
                raw = subprocess.run(
                    ["ioreg", "-r", "-c", "AGXAccelerator", "-d", "1", "-a"],
                    check=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    timeout=1,
                ).stdout
                records = plistlib.loads(raw)
                statistics = records[0].get("PerformanceStatistics", {}) if records else {}
                utilization = statistics.get("Device Utilization %")
                used = statistics.get("In use system memory")
                return float(utilization) if utilization is not None else None, int(used) if used is not None else None, ram_total
        except (OSError, ValueError, IndexError, KeyError, subprocess.SubprocessError, plistlib.InvalidFileException):
            pass
        return None, None, None

    @staticmethod
    def _run(arguments: list[str]) -> str:
        return subprocess.run(
            arguments,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=1,
        ).stdout
