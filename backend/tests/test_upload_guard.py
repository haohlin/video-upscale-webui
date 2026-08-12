import asyncio


def test_chunked_upload_is_rejected_before_downstream_multipart_parser_can_read_past_limit():
    """No Content-Length must not let a tailnet upload consume unbounded disk."""
    from app.upload_guard import UploadBodyGuard

    downstream_reads = 0
    sent: list[dict[str, object]] = []
    messages = [
        {"type": "http.request", "body": b"a" * 4, "more_body": True},
        {"type": "http.request", "body": b"b" * 4, "more_body": False},
    ]

    async def downstream(_scope, receive, _send):
        nonlocal downstream_reads
        while True:
            message = await receive()
            downstream_reads += 1
            if not message.get("more_body"):
                return

    async def receive():
        return messages.pop(0)

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/jobs",
        "headers": [],
    }
    guard = UploadBodyGuard(downstream, max_body_bytes=6, has_disk_reserve=lambda: True)

    asyncio.run(guard(scope, receive, send))

    assert downstream_reads == 1
    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 413


def test_chunked_upload_is_rejected_before_downstream_when_disk_reserve_is_gone():
    """Disk reserve must be checked before multipart parsing starts."""
    from app.upload_guard import UploadBodyGuard

    downstream_called = False
    sent: list[dict[str, object]] = []

    async def downstream(_scope, _receive, _send):
        nonlocal downstream_called
        downstream_called = True

    async def receive():
        raise AssertionError("body must not be read")

    async def send(message):
        sent.append(message)

    guard = UploadBodyGuard(downstream, max_body_bytes=6, has_disk_reserve=lambda: False)
    asyncio.run(
        guard(
            {"type": "http", "method": "POST", "path": "/api/jobs", "headers": []},
            receive,
            send,
        )
    )

    assert downstream_called is False
    assert sent[0]["status"] == 507


def test_concurrent_upload_is_rejected_before_second_body_is_spooled():
    """Allowing concurrent multipart spooling must make this test fail."""
    from app.upload_guard import UploadBodyGuard

    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    second_receive_called = False
    second_sent: list[dict[str, object]] = []

    async def downstream(_scope, receive, _send):
        await receive()
        first_entered.set()
        await release_first.wait()

    first_messages = [{"type": "http.request", "body": b"one", "more_body": False}]

    async def first_receive():
        return first_messages.pop(0)

    async def second_receive():
        nonlocal second_receive_called
        second_receive_called = True
        return {"type": "http.request", "body": b"two", "more_body": False}

    async def first_send(_message):
        return None

    async def second_send(message):
        second_sent.append(message)

    scope = {"type": "http", "method": "POST", "path": "/api/jobs", "headers": []}
    guard = UploadBodyGuard(downstream, max_body_bytes=6, has_disk_reserve=lambda: True)

    async def exercise():
        first = asyncio.create_task(guard(scope, first_receive, first_send))
        await first_entered.wait()
        second = asyncio.create_task(guard(scope, second_receive, second_send))
        await asyncio.sleep(0.01)
        release_first.set()
        await asyncio.gather(first, second)

    asyncio.run(exercise())

    assert second_receive_called is False
    assert second_sent[0]["status"] == 429


def test_stalled_upload_releases_slot_after_idle_timeout():
    """One slow client must not hold the global upload slot indefinitely."""
    from app.upload_guard import UploadBodyGuard

    sent: list[dict[str, object]] = []

    async def downstream(_scope, receive, _send):
        await receive()

    async def receive():
        await asyncio.Event().wait()

    async def send(message):
        sent.append(message)

    guard = UploadBodyGuard(
        downstream,
        max_body_bytes=6,
        has_disk_reserve=lambda: True,
        upload_idle_timeout_seconds=0.01,
        upload_total_timeout_seconds=1,
    )
    scope = {"type": "http", "method": "POST", "path": "/api/jobs", "headers": []}

    asyncio.run(guard(scope, receive, send))

    assert guard._upload_active is False
    assert sent[0]["status"] == 408
