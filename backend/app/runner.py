from __future__ import annotations

import queue
import re
import shlex
import os
import signal
import subprocess
import threading
import time
from pathlib import Path
from shutil import disk_usage
from typing import Callable, Protocol

from .config import Settings
from .domain import Job, PreflightLimits

ProgressReporter = Callable[[int, str], None]
CancellationChecker = Callable[[], bool]
OUTPUT_CHUNK_CHARS = 8 * 1024
OUTPUT_QUEUE_CHUNKS = 128
MAX_OUTPUT_LINE_CHARS = 64 * 1024


class JobCancelled(Exception):
    pass


class RunnerConfigurationError(Exception):
    pass


class VideoRunner(Protocol):
    def preflight(
        self,
        job: Job,
        limits: PreflightLimits,
        report_progress: ProgressReporter,
        is_cancelled: CancellationChecker,
    ) -> None: ...

    def run(
        self,
        job: Job,
        report_progress: ProgressReporter,
        is_cancelled: CancellationChecker,
    ) -> None: ...


class UnavailableRunner:
    health_status = "unavailable"

    def __init__(self, reason: str) -> None:
        self.reason = reason

    def preflight(self, job: Job, limits: PreflightLimits, report_progress: ProgressReporter, is_cancelled: CancellationChecker) -> None:
        raise RunnerConfigurationError(self.reason)

    def run(self, job: Job, report_progress: ProgressReporter, is_cancelled: CancellationChecker) -> None:
        raise RunnerConfigurationError(self.reason)


class SubprocessRunner:
    """Runs the local SeedVR2 adapter without a shell or user-controlled arguments."""

    _progress_pattern = re.compile(r"^PROGRESS\s+(\d{1,3})(?:\s+(.+))?$")
    health_status = "ready"

    def __init__(self, settings: Settings) -> None:
        if not settings.seedvr2_cli:
            raise RunnerConfigurationError("VIDEO_UPSCALE_SEEDVR2_CLI is not configured")
        if not settings.seedvr2_model_dir:
            raise RunnerConfigurationError("VIDEO_UPSCALE_SEEDVR2_MODEL_DIR is not configured")
        default_model = self._model_for_preset(settings, settings.default_profile)
        missing = [
            filename
            for filename in (default_model, settings.seedvr2_vae_model)
            if not (settings.seedvr2_model_dir / filename).is_file()
        ]
        if missing:
            raise RunnerConfigurationError(
                "SeedVR2 model is not ready: " + ", ".join(missing)
            )
        self._settings = settings
        self._adapter_command = self._command_for_adapter(settings.seedvr2_cli, settings.python)

    def preflight(
        self,
        job: Job,
        limits: PreflightLimits,
        report_progress: ProgressReporter,
        is_cancelled: CancellationChecker,
    ) -> None:
        self._require_model_for(job)
        preview_path = job.output_path.parent.parent / "staging" / f"{job.id}-preflight.mp4"
        preview_output = job.output_path.parent.parent / "staging" / f"{job.id}-preflight-output.mp4"
        try:
            report_progress(2, "preflight-media")
            self._make_preview(job.input_path, preview_path, limits, is_cancelled)
            self._execute(
                job,
                input_path=preview_path,
                output_path=preview_output,
                mode="preflight",
                report_progress=report_progress,
                is_cancelled=is_cancelled,
            )
            if not preview_output.is_file():
                raise RuntimeError("SeedVR2 preflight completed without an output video")
        finally:
            preview_path.unlink(missing_ok=True)
            preview_output.unlink(missing_ok=True)

    def run(self, job: Job, report_progress: ProgressReporter, is_cancelled: CancellationChecker) -> None:
        self._require_model_for(job)
        self._execute(
            job,
            input_path=job.input_path,
            output_path=job.output_path,
            mode="full",
            report_progress=report_progress,
            is_cancelled=is_cancelled,
        )
        if not job.output_path.is_file():
            raise RuntimeError("SeedVR2 completed without an output MP4")

    def _make_preview(
        self,
        input_path: Path,
        output_path: Path,
        limits: PreflightLimits,
        is_cancelled: CancellationChecker,
    ) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        command = [
            self._settings.ffmpeg,
            "-protocol_whitelist",
            "file",
            "-hide_banner",
            "-nostats",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(input_path),
            "-t",
            str(limits.max_duration_seconds),
            "-map",
            "0:v:0",
            "-an",
            "-vf",
            f"scale=-2:min({limits.max_height}\\,ih)",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
        self._run_process(
            command,
            None,
            lambda _value, _stage: None,
            is_cancelled,
            monitored_paths=[output_path],
        )

    def _execute(
        self,
        job: Job,
        *,
        input_path: Path,
        output_path: Path,
        mode: str,
        report_progress: ProgressReporter,
        is_cancelled: CancellationChecker,
    ) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        command = [
            *self._adapter_command,
            "--input",
            str(input_path),
            "--output",
            str(output_path),
            "--preset",
            job.preset,
            "--color-correction",
            job.color_correction,
            "--mode",
            mode,
        ]
        command.extend(["--model-dir", str(self._settings.seedvr2_model_dir)])
        temporary_output = output_path.with_name(f"{output_path.stem}.video-only.mp4")
        self._run_process(
            command,
            job.log_path,
            report_progress,
            is_cancelled,
            monitored_paths=[output_path, temporary_output],
        )

    def _run_process(
        self,
        command: list[str],
        log_path: Path | None,
        report_progress: ProgressReporter,
        is_cancelled: CancellationChecker,
        monitored_paths: list[Path] | None = None,
    ) -> None:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        chunks: queue.Queue[str | None] = queue.Queue(maxsize=OUTPUT_QUEUE_CHUNKS)
        collector_shutdown = threading.Event()
        deadline = time.monotonic() + self._settings.max_process_seconds

        def enqueue_output(chunk: str | None) -> bool:
            while not collector_shutdown.is_set():
                try:
                    chunks.put(chunk, timeout=0.1)
                    return True
                except queue.Full:
                    continue
            return False

        def collect_output() -> None:
            assert process.stdout is not None
            try:
                while not collector_shutdown.is_set():
                    chunk = process.stdout.read(OUTPUT_CHUNK_CHARS)
                    if not chunk or not enqueue_output(chunk):
                        break
                enqueue_output(None)
            except (OSError, ValueError):
                return

        collector = threading.Thread(target=collect_output, daemon=True)
        collector.start()
        log_file = log_path.open("a", encoding="utf-8") if log_path else None
        log_bytes_written = log_path.stat().st_size if log_path and log_path.exists() else 0
        process_stopped = False
        pending_output = ""

        def consume_output_line(line: str) -> None:
            nonlocal log_bytes_written
            line = line[:MAX_OUTPUT_LINE_CHARS]
            if log_file and log_bytes_written < self._settings.max_job_log_bytes:
                remaining = self._settings.max_job_log_bytes - log_bytes_written
                encoded = (line + "\n").encode("utf-8")[:remaining]
                persisted = encoded.decode("utf-8", errors="ignore")
                log_file.write(persisted)
                log_file.flush()
                log_bytes_written += len(persisted.encode("utf-8"))
            match = self._progress_pattern.match(line.strip())
            if match:
                report_progress(int(match.group(1)), match.group(2) or "processing")

        try:
            while True:
                if is_cancelled():
                    self._stop_process_group(process)
                    process_stopped = True
                    raise JobCancelled()
                if not self._artifacts_within_limits(monitored_paths or []):
                    self._stop_process_group(process)
                    process_stopped = True
                    raise RuntimeError("Processing exceeded artifact safety limit")
                if time.monotonic() >= deadline:
                    self._stop_process_group(process)
                    process_stopped = True
                    raise RuntimeError("SeedVR2 processing exceeded configured deadline")
                try:
                    chunk = chunks.get(timeout=0.25)
                except queue.Empty:
                    if process.poll() is not None:
                        break
                    continue
                if chunk is None:
                    break
                pending_output += chunk
                while "\n" in pending_output:
                    line, pending_output = pending_output.split("\n", 1)
                    consume_output_line(line)
                if len(pending_output) > MAX_OUTPUT_LINE_CHARS:
                    consume_output_line(pending_output[:MAX_OUTPUT_LINE_CHARS])
                    pending_output = ""
            if pending_output:
                consume_output_line(pending_output)
            try:
                return_code = process.wait(timeout=max(0.001, deadline - time.monotonic()))
            except subprocess.TimeoutExpired as error:
                self._stop_process_group(process)
                process_stopped = True
                raise RuntimeError("SeedVR2 processing exceeded configured deadline") from error
            if return_code != 0:
                raise RuntimeError(f"SeedVR2 adapter exited with code {return_code}")
            if not self._artifacts_within_limits(monitored_paths or []):
                raise RuntimeError("Processing exceeded artifact safety limit")
        finally:
            if not process_stopped and process.poll() is None:
                self._stop_process_group(process)
            collector_shutdown.set()
            while True:
                try:
                    chunks.get_nowait()
                except queue.Empty:
                    break
            collector.join(timeout=1)
            if log_file:
                log_file.close()
            if collector.is_alive():
                raise RuntimeError("SeedVR2 output collector did not stop")

    def _artifacts_within_limits(self, paths: list[Path]) -> bool:
        try:
            if disk_usage(self._settings.data_root).free < self._settings.disk_reserve_bytes:
                return False
            total = sum(path.stat().st_size for path in paths if path.is_file())
        except OSError:
            return False
        return total <= self._settings.max_job_artifact_bytes

    @staticmethod
    def _stop_process_group(process: subprocess.Popen[str]) -> None:
        """Stop adapter and every worker it owns before queue advances."""
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired as error:
            raise RuntimeError("SeedVR2 process group did not stop after SIGKILL") from error

    @staticmethod
    def _command_for_adapter(adapter: str, python: str) -> list[str]:
        command = shlex.split(adapter)
        if not command:
            raise RunnerConfigurationError("VIDEO_UPSCALE_SEEDVR2_CLI is empty")
        if command[0].endswith(".py"):
            if not Path(command[0]).is_file():
                raise RunnerConfigurationError("SeedVR2 adapter script does not exist")
            return [python, *command]
        return command

    @staticmethod
    def _model_for_preset(settings: Settings, preset: str) -> str:
        if preset == "3b-safe":
            return settings.seedvr2_3b_model
        if preset == "7b-fp8-experimental":
            return settings.seedvr2_7b_fp8_model
        raise RunnerConfigurationError(f"Unsupported SeedVR2 preset: {preset}")

    def _require_model_for(self, job: Job) -> None:
        assert self._settings.seedvr2_model_dir is not None
        model = self._model_for_preset(self._settings, job.preset)
        missing = [
            filename
            for filename in (model, self._settings.seedvr2_vae_model)
            if not (self._settings.seedvr2_model_dir / filename).is_file()
        ]
        if missing:
            raise RunnerConfigurationError(
                "SeedVR2 model is not ready: " + ", ".join(missing)
            )
