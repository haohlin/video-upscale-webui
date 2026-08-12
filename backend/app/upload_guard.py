from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any

ASGIApp = Callable[[dict[str, Any], Callable[[], Awaitable[dict[str, Any]]], Callable[[dict[str, Any]], Awaitable[None]]], Awaitable[None]]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]


class UploadGuardError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        self.status = status
        self.detail = detail
        super().__init__(detail)


class UploadBodyGuard:
    """Bounds raw request bytes before Starlette can spool multipart uploads."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        max_body_bytes: int,
        has_disk_reserve: Callable[[], bool],
        has_queue_capacity: Callable[[], bool] | None = None,
        upload_idle_timeout_seconds: float = 30,
        upload_total_timeout_seconds: float = 6 * 60 * 60,
    ) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes
        self.has_disk_reserve = has_disk_reserve
        self.has_queue_capacity = has_queue_capacity or (lambda: True)
        self.upload_idle_timeout_seconds = upload_idle_timeout_seconds
        self.upload_total_timeout_seconds = upload_total_timeout_seconds
        self._upload_active = False

    async def __call__(self, scope: dict[str, Any], receive: Receive, send: Send) -> None:
        if not self._is_job_upload(scope):
            await self.app(scope, receive, send)
            return
        if self._upload_active:
            await self._reject(send, 429, "Another upload is already in progress")
            return
        if not self.has_queue_capacity():
            await self._reject(send, 429, "Processing queue is full")
            return
        if not self.has_disk_reserve():
            await self._reject(send, 507, "Insufficient free disk space for upload")
            return
        try:
            self._validate_content_length(scope)
        except UploadGuardError as error:
            await self._reject(send, error.status, error.detail)
            return

        received = 0
        deadline = time.monotonic() + self.upload_total_timeout_seconds

        async def guarded_receive() -> dict[str, Any]:
            nonlocal received
            if not self.has_disk_reserve():
                raise UploadGuardError(507, "Insufficient free disk space for upload")
            timeout = min(
                self.upload_idle_timeout_seconds,
                deadline - time.monotonic(),
            )
            if timeout <= 0:
                raise UploadGuardError(408, "Upload timed out")
            try:
                message = await asyncio.wait_for(receive(), timeout=timeout)
            except TimeoutError as error:
                raise UploadGuardError(408, "Upload timed out") from error
            if message.get("type") == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_body_bytes:
                    raise UploadGuardError(413, "Upload exceeds configured size limit")
            return message

        self._upload_active = True
        try:
            try:
                await self.app(scope, guarded_receive, send)
            except UploadGuardError as error:
                await self._reject(send, error.status, error.detail)
        finally:
            self._upload_active = False

    @staticmethod
    def _is_job_upload(scope: dict[str, Any]) -> bool:
        return (
            scope.get("type") == "http"
            and scope.get("method") == "POST"
            and scope.get("path") == "/api/jobs"
        )

    def _validate_content_length(self, scope: dict[str, Any]) -> None:
        for name, value in scope.get("headers", []):
            if name.lower() != b"content-length":
                continue
            try:
                content_length = int(value)
            except ValueError as error:
                raise UploadGuardError(400, "Invalid Content-Length") from error
            if content_length < 0:
                raise UploadGuardError(400, "Invalid Content-Length")
            if content_length > self.max_body_bytes:
                raise UploadGuardError(413, "Upload exceeds configured size limit")
            return

    @staticmethod
    async def _reject(send: Send, status: int, detail: str) -> None:
        payload = json.dumps({"detail": detail}).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(payload)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": payload})
