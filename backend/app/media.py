from __future__ import annotations

import json
import math
import subprocess
import tempfile
from fractions import Fraction
from pathlib import Path
from typing import Protocol

from .domain import MediaInfo

MAX_PROBE_OUTPUT_BYTES = 64 * 1024
SELF_CONTAINED_FORMATS = frozenset(
    {"avi", "matroska,webm", "mov,mp4,m4a,3gp,3g2,mj2"}
)


def _parse_frame_rate(value: object) -> float:
    try:
        return float(Fraction(str(value)))
    except (ValueError, ZeroDivisionError):
        return 0.0


class MediaProbe(Protocol):
    def inspect(self, path: Path) -> MediaInfo | dict[str, float | int]: ...


class SubprocessMediaProbe:
    def __init__(self, executable: str, *, timeout_seconds: int = 30) -> None:
        self._executable = executable
        self._timeout_seconds = timeout_seconds

    def inspect(self, path: Path) -> MediaInfo:
        command = [
            self._executable,
            "-protocol_whitelist",
            "file",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-count_frames",
            "-show_entries",
            "stream=width,height,avg_frame_rate,r_frame_rate,nb_frames,nb_read_frames:format=duration,format_name",
            "-of",
            "json",
            str(path),
        ]
        try:
            with tempfile.TemporaryFile() as output:
                result = subprocess.run(
                    command,
                    stdout=output,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=self._timeout_seconds,
                )
                size = output.tell()
                if size > MAX_PROBE_OUTPUT_BYTES:
                    raise ValueError("ffprobe metadata exceeds safety limit")
                output.seek(0)
                stdout = output.read(MAX_PROBE_OUTPUT_BYTES + 1).decode(
                    "utf-8", errors="strict"
                )
        except subprocess.TimeoutExpired as error:
            raise ValueError("ffprobe timed out while validating uploaded video") from error
        if result.returncode != 0:
            raise ValueError("ffprobe could not read uploaded video")
        try:
            payload = json.loads(stdout)
            stream = payload["streams"][0]
            duration = float(payload["format"]["duration"])
            width = int(stream["width"])
            height = int(stream["height"])
            frame_rate = _parse_frame_rate(stream.get("avg_frame_rate"))
            if frame_rate <= 0:
                frame_rate = _parse_frame_rate(stream.get("r_frame_rate"))
            raw_frame_count = stream.get("nb_read_frames")
            if raw_frame_count in (None, "N/A"):
                raw_frame_count = stream.get("nb_frames")
            frame_count = (
                int(raw_frame_count)
                if raw_frame_count not in (None, "N/A")
                else math.ceil(duration * frame_rate)
            )
            format_name = str(payload["format"]["format_name"])
        except (
            IndexError,
            KeyError,
            TypeError,
            ValueError,
            ZeroDivisionError,
            json.JSONDecodeError,
        ) as error:
            raise ValueError("ffprobe did not find a valid video stream") from error
        if duration <= 0 or width <= 0 or height <= 0 or frame_rate <= 0 or frame_count <= 0:
            raise ValueError("ffprobe returned invalid video metadata")
        if format_name not in SELF_CONTAINED_FORMATS:
            raise ValueError("Uploaded media is not a supported self-contained video format")
        return MediaInfo(
            duration_seconds=duration,
            width=width,
            height=height,
            frame_rate=frame_rate,
            frame_count=frame_count,
            format_name=format_name,
        )


def normalize_media_info(value: MediaInfo | dict[str, float | int]) -> MediaInfo:
    if isinstance(value, MediaInfo):
        return value
    try:
        duration = float(value["duration_seconds"])
        width = int(value["width"])
        height = int(value["height"])
        frame_rate = float(value.get("frame_rate", 30.0))
        frame_count = int(value.get("frame_count", math.ceil(duration * frame_rate)))
        format_name = str(value.get("format_name", "mov,mp4,m4a,3gp,3g2,mj2"))
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("media probe returned incomplete video metadata") from error
    if duration <= 0 or width <= 0 or height <= 0 or frame_rate <= 0 or frame_count <= 0:
        raise ValueError("media probe returned invalid video metadata")
    return MediaInfo(
        duration_seconds=duration,
        width=width,
        height=height,
        frame_rate=frame_rate,
        frame_count=frame_count,
        format_name=format_name,
    )
