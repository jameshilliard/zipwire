"""S3 ranges, object consistency, and response ownership without AWS access."""

from __future__ import annotations

import asyncio
import gc
import io
import typing
import zipfile
from contextlib import aclosing
from random import Random

import pytest
from werkzeug import Request, Response

from tests.conftest import (
    has_aiobotocore,
    has_httpx2,
    make_zip,
    needs_aiobotocore,
    needs_httpx2,
)
from zipwire import AsyncReader, AsyncRemoteZip, BadZipFile, RangeRequestUnsupported
from zipwire._constants import STREAM_CHUNK_SIZE

if has_aiobotocore and has_httpx2:
    import httpx2
    from aiobotocore.config import AioConfig
    from aiobotocore.httpxsession import HttpxSession
    from aiobotocore.response import AioHttpxStreamingBody
    from aiobotocore.session import get_session
    from aiobotocore.stub import AioStubber
    from botocore import UNSIGNED
    from botocore.exceptions import ClientError, IncompleteReadError, ResponseStreamingError

    from zipwire.backends import AiobotocoreReader

if typing.TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from aiobotocore.client import AioBaseClient
    from pytest_httpserver import HTTPServer

pytestmark = [needs_aiobotocore, needs_httpx2]

_PARAMS = {"Bucket": "test-bucket", "Key": "test.zip"}
_ETAG = '"test-etag"'


@pytest.fixture
async def s3_client(httpserver: HTTPServer) -> AsyncIterator[AioBaseClient]:
    """Use real SDK serialization and HTTPX2 against a local HTTP server."""
    session = get_session()
    async with session.create_client(
        "s3",
        region_name="us-east-1",
        endpoint_url=httpserver.url_for("/"),
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
        config=AioConfig(
            signature_version=UNSIGNED,
            s3={"addressing_style": "path"},
            retries={"max_attempts": 0},
            http_session_cls=HttpxSession,
        ),
    ) as client:
        yield client


def serve_object(httpserver: HTTPServer, data: bytes) -> list[Request]:
    """Serve an immutable S3 object and record its conditional range GETs."""
    requests: list[Request] = []

    def handler(request: Request) -> Response:
        requests.append(request)
        if request.method == "HEAD":
            return Response(headers={"Content-Length": str(len(data)), "ETag": _ETAG})
        assert request.headers["If-Match"] == _ETAG
        start, end = map(int, request.headers["Range"].removeprefix("bytes=").split("-"))
        assert 0 <= start <= end < len(data)
        return Response(
            data[start : end + 1],
            status=206,
            headers={"Content-Range": f"bytes {start}-{end}/{len(data)}", "ETag": _ETAG},
        )

    httpserver.expect_request("/test-bucket/test.zip").respond_with_handler(handler)
    return requests


class MemoryBody:
    """An async response whose reads and awaited cleanup can be observed."""

    def __init__(self, data: bytes) -> None:
        self.data = io.BytesIO(data)
        self.read_sizes: list[int | None] = []
        self.closed = False
        self.started = asyncio.Event()
        self.pause = False
        self.error: Exception | None = None

    async def read(self, amt: int | None = None) -> bytes:
        assert not self.closed
        self.read_sizes.append(amt)
        self.started.set()
        if self.pause:
            await asyncio.Event().wait()
        await asyncio.sleep(0)
        if self.error is not None:
            raise self.error
        return self.data.read(-1 if amt is None else amt)

    async def __aenter__(self) -> MemoryBody:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await asyncio.sleep(0)
        self.closed = True


class PausedCloseBody(MemoryBody):
    """Keep cleanup pending until the test releases it."""

    def __init__(self, data: bytes) -> None:
        super().__init__(data)
        self.close_started = asyncio.Event()
        self.close_finished = asyncio.Event()
        self.finish_close = asyncio.Event()
        self.close_calls = 0

    async def __aexit__(self, *exc: object) -> None:
        self.close_calls += 1
        self.close_started.set()
        await self.finish_close.wait()
        await super().__aexit__(*exc)
        self.close_finished.set()


def stub_head(stubber: AioStubber, size: int, **extra_args: str) -> None:
    stubber.add_response(
        "head_object",
        {
            "ContentLength": size,
            "ETag": _ETAG,
            "ResponseMetadata": {"HTTPStatusCode": 200, "HTTPHeaders": {}},
        },
        {**_PARAMS, **extra_args},
    )


def stub_range(
    stubber: AioStubber,
    body: MemoryBody | AioHttpxStreamingBody,
    size: int,
    *,
    offset: int = 0,
    length: int | None = None,
    response_overrides: dict[str, object] | None = None,
    extra_args: dict[str, str] | None = None,
) -> None:
    if length is None:
        length = size
    stubber.add_response(
        "get_object",
        {
            "Body": body,
            "ContentLength": length,
            "ContentRange": f"bytes {offset}-{offset + length - 1}/{size}",
            "ETag": _ETAG,
            "ResponseMetadata": {"HTTPStatusCode": 206, "HTTPHeaders": {}},
            **(response_overrides or {}),
        },
        {
            **_PARAMS,
            **(extra_args or {}),
            "Range": f"bytes={offset}-{offset + length - 1}",
            "IfMatch": _ETAG,
        },
    )


@pytest.mark.parametrize("compression", [zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED])
async def test_read_archive(
    httpserver: HTTPServer, s3_client: AioBaseClient, compression: int
) -> None:
    content = Random(0).randbytes(70_000)
    data = make_zip(
        {"small.txt": b"hello", "empty": b"", "large": content}, compression=compression
    )
    requests = serve_object(httpserver, data)
    bodies: list[AioHttpxStreamingBody] = []

    def record_body(parsed: dict[str, object], **kwargs: object) -> None:
        body = parsed["Body"]
        assert isinstance(body, AioHttpxStreamingBody)
        bodies.append(body)

    s3_client.meta.events.register("after-call.s3.GetObject", record_body)
    reader = AiobotocoreReader("test-bucket", "test.zip", client=s3_client)
    assert isinstance(reader, AsyncReader)
    async with AsyncRemoteZip(reader) as archive:
        assert archive.url == "s3://test-bucket/test.zip"
        assert await archive.read("small.txt") == b"hello"
        assert await archive.read("empty") == b""
        dest = io.BytesIO()
        await archive.read_into("large", dest)
        assert dest.getvalue() == content
        assert bodies
        assert all(body.raw_stream.is_closed for body in bodies)
    assert sum(request.method == "HEAD" for request in requests) == 1
    # Closing the archive must leave the caller's client usable.
    assert (await s3_client.head_object(**_PARAMS))["ContentLength"] == len(data)


async def test_extraction_failure_closes_body(
    httpserver: HTTPServer, s3_client: AioBaseClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = make_zip({"large": b"x" * (STREAM_CHUNK_SIZE + 1), "small.txt": b"hello"})
    serve_object(httpserver, data)
    bodies: list[AioHttpxStreamingBody] = []

    def record_body(parsed: dict[str, object], **kwargs: object) -> None:
        body = parsed["Body"]
        assert isinstance(body, AioHttpxStreamingBody)
        bodies.append(body)

    def failing_write(data: bytes) -> int:
        raise OSError("destination write failed")

    s3_client.meta.events.register("after-call.s3.GetObject", record_body)
    reader = AiobotocoreReader("test-bucket", "test.zip", client=s3_client)
    async with AsyncRemoteZip(reader) as archive:
        dest = io.BytesIO()
        monkeypatch.setattr(dest, "write", failing_write)
        with pytest.raises(OSError, match="destination write failed"):
            await archive.read_into("large", dest)
        assert bodies[-1].raw_stream.is_closed
        assert await archive.read("small.txt") == b"hello"


async def test_empty_archive(httpserver: HTTPServer, s3_client: AioBaseClient) -> None:
    requests = serve_object(httpserver, make_zip({}))
    reader = AiobotocoreReader("test-bucket", "test.zip", client=s3_client)
    async with AsyncRemoteZip(reader) as rz:
        assert rz.namelist() == []
    assert len(requests) == 2  # No GET for the empty central directory.


async def test_head_and_extra_args(s3_client: AioBaseClient) -> None:
    extra_args = {
        "VersionId": "version-1",
        "RequestPayer": "requester",
        "ExpectedBucketOwner": "123456789012",
        "SSECustomerAlgorithm": "AES256",
        "SSECustomerKey": "encryption-key",
        "SSECustomerKeyMD5": "key-md5",
        "ChecksumMode": "ENABLED",
    }
    reader = AiobotocoreReader("test-bucket", "test.zip", client=s3_client, extra_args=extra_args)
    body = MemoryBody(b"data")
    with AioStubber(s3_client) as stubber:
        stub_head(stubber, 4, **extra_args)
        stub_range(stubber, body, 4, extra_args=extra_args.copy())
        extra_args["VersionId"] = "changed-after-construction"
        async with reader:
            headers = await reader.head()
            assert headers["content-length"] == "4"
            assert headers["accept-ranges"] == "bytes"
            headers["etag"] = "changed-by-caller"
            assert (await reader.head())["etag"] == _ETAG
            assert (await reader.read_range(0, 100))[0] == b"data"
        stubber.assert_no_pending_responses()
    assert body.closed


@pytest.mark.parametrize("method", ["read_range", "stream_range"])
async def test_empty_range(s3_client: AioBaseClient, method: str) -> None:
    with AioStubber(s3_client) as stubber:
        stub_head(stubber, 0)
        async with AiobotocoreReader("test-bucket", "test.zip", client=s3_client) as reader:
            if method == "read_range":
                assert await reader.read_range(0, 0) == (b"", {"content-length": "0"})
            else:
                assert [chunk async for chunk in reader.stream_range(0, 0)] == []
        stubber.assert_no_pending_responses()


@pytest.mark.parametrize("offset,length", [(-1, 1), (0, -1), (5, 1), (4, 1)])
async def test_invalid_range(s3_client: AioBaseClient, offset: int, length: int) -> None:
    with AioStubber(s3_client) as stubber:
        if offset >= 0 and length >= 0:
            stub_head(stubber, 4)
        async with AiobotocoreReader("test-bucket", "test.zip", client=s3_client) as reader:
            with pytest.raises(ValueError, match="Range"):
                await reader.read_range(offset, length)
        stubber.assert_no_pending_responses()


@pytest.mark.parametrize(
    "overrides",
    [
        {"ResponseMetadata": {"HTTPStatusCode": 200, "HTTPHeaders": {}}},
        {"ContentRange": "bytes 1-4/4"},
        {"ContentRange": "bytes 0-3/100"},
        {"ContentLength": 100},
    ],
)
@pytest.mark.parametrize("streaming", [False, True])
async def test_invalid_response(
    s3_client: AioBaseClient, overrides: dict[str, object], streaming: bool
) -> None:
    body = MemoryBody(b"data")
    with AioStubber(s3_client) as stubber:
        stub_head(stubber, 4)
        stub_range(stubber, body, 4, response_overrides=overrides)
        async with AiobotocoreReader("test-bucket", "test.zip", client=s3_client) as reader:
            with pytest.raises(RangeRequestUnsupported):
                if streaming:
                    _ = [chunk async for chunk in reader.stream_range(0, 4)]
                else:
                    await reader.read_range(0, 4)
            assert body.closed
            assert not body.read_sizes


async def test_object_changed(s3_client: AioBaseClient) -> None:
    with AioStubber(s3_client) as stubber:
        stub_head(stubber, 4)
        stubber.add_client_error(
            "get_object",
            service_error_code="PreconditionFailed",
            http_status_code=412,
            expected_params={**_PARAMS, "Range": "bytes=0-3", "IfMatch": _ETAG},
        )
        async with AiobotocoreReader("test-bucket", "test.zip", client=s3_client) as reader:
            with pytest.raises(ClientError, match="PreconditionFailed"):
                await reader.read_range(0, 4)


async def test_streaming_chunks(s3_client: AioBaseClient) -> None:
    data = b"x" * (STREAM_CHUNK_SIZE + 1)
    body = MemoryBody(data)
    with AioStubber(s3_client) as stubber:
        stub_head(stubber, len(data))
        stub_range(stubber, body, len(data))
        async with AiobotocoreReader("test-bucket", "test.zip", client=s3_client) as reader:
            async with aclosing(reader.stream_range(0, len(data))) as stream:
                assert await anext(stream) == data[:STREAM_CHUNK_SIZE]
                assert body.read_sizes == [STREAM_CHUNK_SIZE]
                assert await anext(stream) == b"x"
                with pytest.raises(StopAsyncIteration):
                    await anext(stream)
            assert body.closed


@pytest.mark.parametrize("close_reader", [False, True])
async def test_unfinished_stream(s3_client: AioBaseClient, close_reader: bool) -> None:
    body = MemoryBody(b"data")
    with AioStubber(s3_client) as stubber:
        stub_head(stubber, 4)
        stub_range(stubber, body, 4)
        async with AiobotocoreReader("test-bucket", "test.zip", client=s3_client) as reader:
            async with aclosing(reader.stream_range(0, 4)) as stream:
                assert await anext(stream) == b"data"
                assert not body.closed
                if close_reader:
                    await reader.close()
                    assert body.closed
            assert body.closed
        await reader.close()  # Idempotent even after a stream was abandoned.


async def test_reader_close_waits_for_stream_finalizer(
    s3_client: AioBaseClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = b"x" * (STREAM_CHUNK_SIZE + 1)
    cleanup = PausedCloseBody(data)
    transport = httpx2.ByteStream(data)
    monkeypatch.setattr(transport, "aclose", cleanup.__aexit__)
    body = AioHttpxStreamingBody(httpx2.Response(206, stream=transport), len(data))
    with AioStubber(s3_client) as stubber:
        stub_head(stubber, len(data))
        stub_range(stubber, body, len(data))
        async with (
            AiobotocoreReader("test-bucket", "test.zip", client=s3_client) as reader,
            asyncio.TaskGroup() as tasks,
        ):
            try:
                # Dropping the iterator schedules its finalizer independently
                # of this task, even without concurrent use by the caller.
                async for chunk in reader.stream_range(0, len(data)):
                    assert len(chunk) == STREAM_CHUNK_SIZE
                    break
                gc.collect()
                await asyncio.wait_for(cleanup.close_started.wait(), timeout=5)
                closing = tasks.create_task(reader.close())
                done, _ = await asyncio.wait({closing}, timeout=0.01)
                assert not done
                assert not cleanup.closed
            finally:
                cleanup.finish_close.set()
                await asyncio.wait_for(cleanup.close_finished.wait(), timeout=5)
            await closing
            assert cleanup.closed
            assert cleanup.close_calls == 1


async def test_overlapping_closes_survive_cancelled_waiter(s3_client: AioBaseClient) -> None:
    body = PausedCloseBody(b"data")
    with AioStubber(s3_client) as stubber:
        stub_head(stubber, 4)
        stub_range(stubber, body, 4)
        async with (
            AiobotocoreReader("test-bucket", "test.zip", client=s3_client) as reader,
            aclosing(reader.stream_range(0, 4)) as stream,
            asyncio.TaskGroup() as tasks,
        ):
            assert await anext(stream) == b"data"
            stream_close = tasks.create_task(stream.aclose())
            try:
                await asyncio.wait_for(body.close_started.wait(), timeout=5)
                reader_close = tasks.create_task(reader.close())
                for _ in range(2):
                    stream_close.cancel()
                    await asyncio.sleep(0)
                done, _ = await asyncio.wait({stream_close, reader_close}, timeout=0.01)
                assert not done
            finally:
                body.finish_close.set()
            with pytest.raises(asyncio.CancelledError):
                await stream_close
            await reader_close
            assert body.closed
            assert body.close_calls == 1


@pytest.mark.parametrize("streaming", [False, True])
async def test_read_failure_closes_body(s3_client: AioBaseClient, streaming: bool) -> None:
    body = MemoryBody(b"data")
    body.error = ResponseStreamingError(error="truncated response")
    with AioStubber(s3_client) as stubber:
        stub_head(stubber, 4)
        stub_range(stubber, body, 4)
        async with AiobotocoreReader("test-bucket", "test.zip", client=s3_client) as reader:
            with pytest.raises(ResponseStreamingError):
                if streaming:
                    _ = [chunk async for chunk in reader.stream_range(0, 4)]
                else:
                    await reader.read_range(0, 4)
            assert body.closed


@pytest.mark.parametrize("streaming", [False, True])
async def test_truncated_sdk_body(s3_client: AioBaseClient, streaming: bool) -> None:
    response = httpx2.Response(
        206, headers={"Content-Length": "4"}, stream=httpx2.ByteStream(b"ab")
    )
    body = AioHttpxStreamingBody(response, 4)
    with AioStubber(s3_client) as stubber:
        stub_head(stubber, 4)
        stub_range(stubber, body, 4)
        async with AiobotocoreReader("test-bucket", "test.zip", client=s3_client) as reader:
            with pytest.raises(IncompleteReadError):
                if streaming:
                    _ = [chunk async for chunk in reader.stream_range(0, 4)]
                else:
                    await reader.read_range(0, 4)
            assert response.is_closed


@pytest.mark.parametrize("streaming", [False, True])
async def test_cancellation_closes_body(s3_client: AioBaseClient, streaming: bool) -> None:
    body = MemoryBody(b"data")
    body.pause = True
    with AioStubber(s3_client) as stubber:
        stub_head(stubber, 4)
        stub_range(stubber, body, 4)
        async with (
            AiobotocoreReader("test-bucket", "test.zip", client=s3_client) as reader,
            asyncio.TaskGroup() as tasks,
            aclosing(reader.stream_range(0, 4)) as stream,
        ):
            task = tasks.create_task(anext(stream) if streaming else reader.read_range(0, 4))
            await asyncio.wait_for(body.started.wait(), timeout=5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert body.closed


@pytest.mark.parametrize(
    "operation,sdk_body",
    [("read", False), ("stream", False), ("reader", False), ("stream", True), ("reader", True)],
)
async def test_cancellation_during_close(
    s3_client: AioBaseClient, monkeypatch: pytest.MonkeyPatch, operation: str, sdk_body: bool
) -> None:
    data = b"x" * (STREAM_CHUNK_SIZE + 1)
    body = PausedCloseBody(data)
    response_body: MemoryBody | AioHttpxStreamingBody = body
    if sdk_body:
        transport = httpx2.ByteStream(data)
        monkeypatch.setattr(transport, "aclose", body.__aexit__)
        response_body = AioHttpxStreamingBody(httpx2.Response(206, stream=transport), len(data))
    with AioStubber(s3_client) as stubber:
        stub_head(stubber, len(data))
        stub_range(stubber, response_body, len(data))
        async with (
            AiobotocoreReader("test-bucket", "test.zip", client=s3_client) as reader,
            aclosing(reader.stream_range(0, len(data))) as stream,
            asyncio.TaskGroup() as tasks,
        ):
            if operation == "read":
                closing = reader.read_range(0, len(data))
            else:
                assert await anext(stream) == data[:STREAM_CHUNK_SIZE]
                closing = reader.close() if operation == "reader" else stream.aclose()
            task = tasks.create_task(closing)
            try:
                await asyncio.wait_for(body.close_started.wait(), timeout=5)
                for _ in range(2):
                    task.cancel()
                    await asyncio.sleep(0)
                    assert not task.done()
                    assert not body.closed
            finally:
                body.finish_close.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert body.closed
            await reader.close()
        assert body.close_calls == 1


@pytest.mark.parametrize("close_reader", [False, True])
async def test_timeout_closes_body(s3_client: AioBaseClient, close_reader: bool) -> None:
    body = MemoryBody(b"data")
    with AioStubber(s3_client) as stubber:
        stub_head(stubber, 4)
        stub_range(stubber, body, 4)
        async with (
            AiobotocoreReader("test-bucket", "test.zip", client=s3_client) as reader,
            aclosing(reader.stream_range(0, 4)) as stream,
        ):
            assert await anext(stream) == b"data"
            with pytest.raises(TimeoutError):
                async with asyncio.timeout(0):
                    if close_reader:
                        await reader.close()
                    else:
                        await anext(stream)
            assert body.closed


async def test_close_failure_closes_other_responses(
    s3_client: AioBaseClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    bodies = [MemoryBody(b"data"), MemoryBody(b"data")]

    async def failing_exit(body: MemoryBody, *exc: object) -> None:
        await asyncio.sleep(0)
        body.closed = True
        raise OSError("close failed")

    monkeypatch.setattr(MemoryBody, "__aexit__", failing_exit)
    with AioStubber(s3_client) as stubber:
        stub_head(stubber, 4)
        for body in bodies:
            stub_range(stubber, body, 4)
        async with (
            AiobotocoreReader("test-bucket", "test.zip", client=s3_client) as reader,
            aclosing(reader.stream_range(0, 4)) as first,
            aclosing(reader.stream_range(0, 4)) as second,
        ):
            assert await anext(first) == b"data"
            assert await anext(second) == b"data"
            with pytest.raises(OSError, match="close failed"):
                await reader.close()
            assert all(body.closed for body in bodies)


async def test_invalid_archive_closes_body(s3_client: AioBaseClient) -> None:
    body = MemoryBody(b"not a zip")
    with AioStubber(s3_client) as stubber:
        stub_head(stubber, 9)
        stub_range(stubber, body, 9)
        async with AiobotocoreReader("test-bucket", "test.zip", client=s3_client) as reader:
            with pytest.raises(BadZipFile):
                async with AsyncRemoteZip(reader):
                    pytest.fail("Invalid ZIP accepted")
            assert body.closed


async def test_closed_reader(s3_client: AioBaseClient) -> None:
    reader = AiobotocoreReader("test-bucket", "test.zip", client=s3_client)
    await reader.close()
    with pytest.raises(ValueError, match="closed"):
        await reader.head()
    with pytest.raises(ValueError, match="closed"):
        async with reader:
            pytest.fail("Closed reader entered")


@pytest.mark.parametrize("argument", ["Bucket", "Key", "Range", "IfMatch", "PartNumber"])
def test_unsupported_extra_args(s3_client: AioBaseClient, argument: str) -> None:
    with pytest.raises(ValueError, match=argument):
        AiobotocoreReader("test-bucket", "test.zip", client=s3_client, extra_args={argument: "x"})


def test_url_preserves_key(s3_client: AioBaseClient) -> None:
    reader = AiobotocoreReader("test-bucket", "/a b/%20?#.zip", client=s3_client)
    assert reader.url == "s3://test-bucket//a%20b/%2520%3F%23.zip"
