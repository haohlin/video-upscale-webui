from __future__ import annotations

import errno
import json
import os
import re
import secrets
import stat
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Callable, Mapping

from .config import Settings


SESSION_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{32,128}\Z")
MAX_SESSION_METADATA_BYTES = 64 * 1024
MAX_SESSION_OPTIONS_BYTES = 32 * 1024


class UploadSessionError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


@dataclass(frozen=True)
class FinalizedUpload:
    id: str
    filename: str
    total_bytes: int
    accepted_bytes: int
    expires_at: str
    path: Path
    options: dict[str, object]


class UploadSessionService:
    """Disk-backed sequential upload sessions rooted in the staging directory."""

    def __init__(
        self,
        settings: Settings,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.settings = settings
        self.staging = settings.data_root / "staging"
        self._now = now or (lambda: datetime.now(UTC))
        self._session_lock = threading.RLock()
        self.staging.mkdir(parents=True, exist_ok=True)

    def create(
        self,
        *,
        filename: str,
        total_bytes: int,
        options: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        if type(total_bytes) is not int or not 0 < total_bytes <= self.settings.max_upload_bytes:
            raise UploadSessionError(413, "Upload exceeds configured size limit")
        normalized_filename = self._normalize_filename(filename)
        normalized_options = self._normalize_options(options)
        with self._session_lock:
            self._cleanup_expired()
            for _ in range(10):
                session_id = secrets.token_urlsafe(32)
                data_path = self._data_path(session_id)
                try:
                    self._create_empty_file(data_path)
                except FileExistsError:
                    continue
                expires_at = self._utc_now() + timedelta(
                    seconds=self.settings.upload_session_ttl_seconds
                )
                record = {
                    "id": session_id,
                    "filename": normalized_filename,
                    "total_bytes": total_bytes,
                    "accepted_bytes": 0,
                    "expires_at": expires_at.isoformat(),
                    "options": normalized_options,
                }
                try:
                    self._write_metadata(session_id, record)
                except Exception:
                    data_path.unlink(missing_ok=True)
                    raise
                return self._public(record)
        raise RuntimeError("Could not allocate upload session")

    def status(self, session_id: str) -> dict[str, object]:
        with self._session_lock:
            return self._public(self._load(session_id))

    def append(self, session_id: str, *, offset: int, data: bytes) -> dict[str, object]:
        if type(offset) is not int or offset < 0:
            raise UploadSessionError(400, "Upload offset is invalid")
        if not isinstance(data, bytes) or not data:
            raise UploadSessionError(400, "Upload chunk is empty or invalid")
        with self._session_lock:
            record = self._load(session_id)
            accepted = self._integer(record, "accepted_bytes")
            total = self._integer(record, "total_bytes")
            if offset != accepted:
                raise UploadSessionError(409, "Upload offset does not match accepted bytes")
            if len(data) > total - accepted:
                raise UploadSessionError(413, "Upload exceeds declared session size")
            self._append_data(session_id, accepted, data)
            record["accepted_bytes"] = accepted + len(data)
            self._write_metadata(session_id, record)
            return self._public(record)

    def finalize(self, session_id: str) -> FinalizedUpload:
        with self._session_lock:
            record = self._load(session_id)
            accepted = self._integer(record, "accepted_bytes")
            total = self._integer(record, "total_bytes")
            if accepted != total:
                raise UploadSessionError(409, "Upload is incomplete")
            path = self._data_path(session_id)
            self._verify_data_file(path, accepted)
            return FinalizedUpload(
                id=session_id,
                filename=self._string(record, "filename"),
                total_bytes=total,
                accepted_bytes=accepted,
                expires_at=self._string(record, "expires_at"),
                path=path,
                options=self._options(record),
            )

    def discard(self, session_id: str) -> None:
        with self._session_lock:
            record = self._load(session_id)
            self._remove_files(self._string(record, "id"))

    def _load(self, session_id: str) -> dict[str, object]:
        self._validate_session_id(session_id)
        path = self._metadata_path(session_id)
        try:
            record = self._read_json(path)
        except FileNotFoundError as error:
            raise UploadSessionError(404, "Upload session not found") from error
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise UploadSessionError(409, "Upload session metadata is invalid") from error
        except OSError as error:
            raise UploadSessionError(409, "Unsafe upload staging file") from error
        if not isinstance(record, dict) or self._string(record, "id") != session_id:
            raise UploadSessionError(409, "Upload session metadata is invalid")
        expires_at = self._parse_expiry(record)
        if expires_at <= self._utc_now():
            self._remove_files(session_id)
            raise UploadSessionError(410, "Upload session has expired")
        accepted = self._integer(record, "accepted_bytes")
        total = self._integer(record, "total_bytes")
        if accepted < 0 or total <= 0 or accepted > total:
            raise UploadSessionError(409, "Upload session metadata is invalid")
        self._string(record, "filename")
        self._options(record)
        data_size = self._data_size(session_id)
        if data_size < accepted or data_size > total:
            raise UploadSessionError(409, "Unsafe upload staging file")
        if data_size > accepted:
            record["accepted_bytes"] = data_size
            self._write_metadata(session_id, record)
        return record

    def _append_data(self, session_id: str, accepted: int, data: bytes) -> None:
        path = self._data_path(session_id)
        fd = self._open_existing_data(path)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size != accepted:
                raise UploadSessionError(409, "Unsafe upload staging file")
            written = 0
            while written < len(data):
                count = os.write(fd, data[written:])
                if count <= 0:
                    raise OSError("Could not write upload staging data")
                written += count
            os.fsync(fd)
        finally:
            os.close(fd)

    def _verify_data_file(self, path: Path, accepted: int) -> None:
        fd = self._open_existing_data(path)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size != accepted:
                raise UploadSessionError(409, "Unsafe upload staging file")
        finally:
            os.close(fd)

    def _data_size(self, session_id: str) -> int:
        path = self._data_path(session_id)
        fd = self._open_existing_data(path)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise UploadSessionError(409, "Unsafe upload staging file")
            return info.st_size
        finally:
            os.close(fd)

    def _open_existing_data(self, path: Path) -> int:
        nofollow = getattr(os, "O_NOFOLLOW", None)
        if nofollow is None:
            raise UploadSessionError(409, "Unsafe upload staging file")
        try:
            return os.open(path, os.O_WRONLY | os.O_APPEND | nofollow)
        except OSError as error:
            if error.errno in {errno.ELOOP, errno.ENOENT}:
                raise UploadSessionError(409, "Unsafe upload staging file") from error
            raise

    def _create_empty_file(self, path: Path) -> None:
        nofollow = getattr(os, "O_NOFOLLOW", None)
        if nofollow is None:
            raise UploadSessionError(409, "Unsafe upload staging file")
        fd = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow,
            0o600,
        )
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _write_metadata(self, session_id: str, record: dict[str, object]) -> None:
        payload = json.dumps(record, separators=(",", ":"), sort_keys=True).encode("utf-8")
        if len(payload) > MAX_SESSION_METADATA_BYTES:
            raise UploadSessionError(413, "Upload session metadata is too large")
        temporary = self.staging / f".{session_id}.{secrets.token_hex(8)}.tmp"
        nofollow = getattr(os, "O_NOFOLLOW", None)
        if nofollow is None:
            raise UploadSessionError(409, "Unsafe upload staging file")
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow,
            0o600,
        )
        try:
            written = 0
            while written < len(payload):
                count = os.write(fd, payload[written:])
                if count <= 0:
                    raise OSError("Could not write upload session metadata")
                written += count
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.replace(temporary, self._metadata_path(session_id))
            self._fsync_staging()
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    def _read_json(self, path: Path) -> dict[str, object]:
        nofollow = getattr(os, "O_NOFOLLOW", None)
        if nofollow is None:
            raise OSError("O_NOFOLLOW is unavailable")
        fd = os.open(path, os.O_RDONLY | nofollow)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise OSError("metadata is not a regular file")
            if info.st_size > MAX_SESSION_METADATA_BYTES:
                raise OSError("metadata exceeds size limit")
            raw = b""
            while chunk := os.read(fd, 64 * 1024):
                raw += chunk
        finally:
            os.close(fd)
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise OSError("metadata is not an object")
        return value

    def _cleanup_expired(self) -> None:
        with self._session_lock:
            for metadata_path in self.staging.glob("*.json"):
                session_id = metadata_path.stem
                if not SESSION_ID_PATTERN.fullmatch(session_id):
                    continue
                try:
                    record = self._read_json(metadata_path)
                    if self._parse_expiry(record) <= self._utc_now():
                        self._remove_files(session_id)
                except (OSError, ValueError, json.JSONDecodeError, UploadSessionError):
                    continue

    def _remove_files(self, session_id: str) -> None:
        self._metadata_path(session_id).unlink(missing_ok=True)
        self._data_path(session_id).unlink(missing_ok=True)
        self._fsync_staging()

    def _fsync_staging(self) -> None:
        fd = os.open(self.staging, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _metadata_path(self, session_id: str) -> Path:
        self._validate_session_id(session_id)
        return self.staging / f"{session_id}.json"

    def _data_path(self, session_id: str) -> Path:
        self._validate_session_id(session_id)
        return self.staging / f"{session_id}.part"

    @staticmethod
    def _normalize_filename(filename: str) -> str:
        if not isinstance(filename, str):
            raise UploadSessionError(400, "Upload filename is invalid")
        normalized = filename.replace("\\", "/").rsplit("/", 1)[-1].strip()
        if not normalized or normalized in {".", ".."}:
            raise UploadSessionError(400, "Upload filename is invalid")
        return normalized

    @staticmethod
    def _normalize_options(options: Mapping[str, object] | None) -> dict[str, object]:
        if options is None:
            return {}
        if not isinstance(options, Mapping):
            raise UploadSessionError(400, "Upload options are invalid")
        try:
            encoded = json.dumps(dict(options), separators=(",", ":")).encode("utf-8")
            value = json.loads(encoded)
        except (TypeError, ValueError) as error:
            raise UploadSessionError(400, "Upload options are invalid") from error
        if len(encoded) > MAX_SESSION_OPTIONS_BYTES:
            raise UploadSessionError(413, "Upload options are too large")
        if not isinstance(value, dict):
            raise UploadSessionError(400, "Upload options are invalid")
        return value

    @staticmethod
    def _validate_session_id(session_id: str) -> None:
        if not isinstance(session_id, str) or not SESSION_ID_PATTERN.fullmatch(session_id):
            raise UploadSessionError(404, "Upload session not found")

    @staticmethod
    def _integer(record: dict[str, object], name: str) -> int:
        value = record.get(name)
        if type(value) is not int:
            raise UploadSessionError(409, "Upload session metadata is invalid")
        return value

    @staticmethod
    def _string(record: dict[str, object], name: str) -> str:
        value = record.get(name)
        if not isinstance(value, str):
            raise UploadSessionError(409, "Upload session metadata is invalid")
        return value

    @staticmethod
    def _options(record: dict[str, object]) -> dict[str, object]:
        value = record.get("options")
        if not isinstance(value, dict):
            raise UploadSessionError(409, "Upload session metadata is invalid")
        return dict(value)

    def _parse_expiry(self, record: dict[str, object]) -> datetime:
        try:
            value = datetime.fromisoformat(self._string(record, "expires_at"))
        except ValueError as error:
            raise UploadSessionError(409, "Upload session metadata is invalid") from error
        if value.tzinfo is None:
            raise UploadSessionError(409, "Upload session metadata is invalid")
        return value.astimezone(UTC)

    def _utc_now(self) -> datetime:
        value = self._now()
        if value.tzinfo is None:
            raise ValueError("Upload session clock must return an aware datetime")
        return value.astimezone(UTC)

    @staticmethod
    def _public(record: dict[str, object]) -> dict[str, object]:
        return {
            "id": UploadSessionService._string(record, "id"),
            "filename": UploadSessionService._string(record, "filename"),
            "total_bytes": UploadSessionService._integer(record, "total_bytes"),
            "accepted_bytes": UploadSessionService._integer(record, "accepted_bytes"),
            "expires_at": UploadSessionService._string(record, "expires_at"),
        }
