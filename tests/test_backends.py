"""Integration tests for backends using pytest-httpserver."""

from __future__ import annotations

import asyncio
import io
import struct
import threading
import typing
import zipfile
from contextlib import AsyncExitStack, closing

import pytest
import urllib3
from werkzeug import Request, Response

from tests.conftest import (
    has_aiohttp,
    has_httpx2,
    has_requests,
    make_zip,
    needs_aiohttp,
    needs_httpx2,
    needs_requests,
)
from tests.test_parity import kind as kind
from tests.test_parity import read
from zipwire import (
    AsyncReader,
    AsyncRemoteWheel,
    AsyncRemoteZip,
    SyncReader,
    SyncRemoteWheel,
    SyncRemoteZip,
    backends,
)
from zipwire._constants import STREAM_CHUNK_SIZE
from zipwire._errors import RangeRequestUnsupported
from zipwire.backends import AsyncFileReader, FileReader, Urllib3Reader

if typing.TYPE_CHECKING:
    from pathlib import Path

    from pytest_httpserver import HTTPServer

if has_httpx2:
    import httpx2

    from zipwire.backends import Httpx2AsyncReader, Httpx2SyncReader

if has_requests:
    import requests

    from zipwire.backends import RequestsReader

if has_aiohttp:
    import aiohttp

    from zipwire.backends import AiohttpReader


def range_handler(zip_data: bytes):
    """Create a handler that supports HEAD and Range GET requests."""

    def handler(request: Request) -> Response:
        if request.method == "HEAD":
            return Response(
                status=200,
                headers={
                    "Content-Length": str(len(zip_data)),
                    "Accept-Ranges": "bytes",
                },
            )
        # GET with Range header
        range_header = request.headers.get("Range", "")
        if range_header.startswith("bytes="):
            range_spec = range_header[6:]
            if range_spec.startswith("-"):
                # Suffix range: bytes=-N
                suffix_len = int(range_spec[1:])
                start = max(0, len(zip_data) - suffix_len)
                end = len(zip_data) - 1
            else:
                parts = range_spec.split("-")
                start = int(parts[0])
                end = int(parts[1]) if parts[1] else len(zip_data) - 1
            if not 0 <= start <= end or start >= len(zip_data):
                return Response(status=416)
            end = min(end, len(zip_data) - 1)
            chunk = zip_data[start : end + 1]
            return Response(
                chunk,
                status=206,
                headers={
                    "Content-Range": f"bytes {start}-{end}/{len(zip_data)}",
                    "Content-Length": str(len(chunk)),
                },
            )
        return Response(zip_data, status=200)

    return handler


@needs_httpx2
@pytest.mark.parametrize("size", [0, 1, 21])
async def test_undersized_archive(kind: str, size: int) -> None:
    methods: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        methods.append(request.method)
        if request.method == "HEAD":
            return httpx2.Response(
                200, headers={"content-length": str(size), "accept-ranges": "bytes"}
            )
        return httpx2.Response(416)

    url = "https://example.com/demo-1.0-py3-none-any.whl"
    async with AsyncExitStack() as stack:
        if kind.startswith("async"):
            client = await stack.enter_async_context(
                httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
            )
            cls = AsyncRemoteWheel if kind.endswith("wheel") else AsyncRemoteZip
            with pytest.raises(zipfile.BadZipFile):
                await stack.enter_async_context(cls(Httpx2AsyncReader(url, client=client)))
        else:
            client = stack.enter_context(httpx2.Client(transport=httpx2.MockTransport(handler)))
            cls = SyncRemoteWheel if kind.endswith("wheel") else SyncRemoteZip
            with pytest.raises(zipfile.BadZipFile):
                stack.enter_context(cls(Httpx2SyncReader(url, client=client)))
    assert methods == ["HEAD"]


@needs_httpx2
@pytest.mark.parametrize("long_name", [False, True])
async def test_empty_entry_with_long_header(
    kind: str, long_name: bool, httpserver: HTTPServer
) -> None:
    name = "x" * 2048 if long_name else "empty"
    info = zipfile.ZipInfo(name)
    info.extra = struct.pack("<HH", 0xCAFE, 2048) + b"x" * 2048
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as writer:
        writer.writestr(info, b"")
        # Keep the empty entry outside the wheel reader's cached tail.
        writer.writestr("padding", b"p" * 200000)
    data = buffer.getvalue()
    with zipfile.ZipFile(io.BytesIO(data)) as reference:
        expected = reference.read(name)
    path = "/demo-1.0-py3-none-any.whl"
    httpserver.expect_request(path).respond_with_handler(range_handler(data))
    url = httpserver.url_for(path)
    async with AsyncExitStack() as stack:
        if kind.startswith("async"):
            cls = AsyncRemoteWheel if kind.endswith("wheel") else AsyncRemoteZip
            archive = await stack.enter_async_context(cls(Httpx2AsyncReader(url)))
        else:
            cls = SyncRemoteWheel if kind.endswith("wheel") else SyncRemoteZip
            archive = stack.enter_context(cls(Httpx2SyncReader(url)))
        assert await read(archive, name) == expected == b""
        assert await read(archive, name, into=True) == expected


def no_range_handler(request: Request) -> Response:
    """Handler that does NOT advertise range support and returns 200 for GETs."""
    if request.method == "HEAD":
        return Response(status=200, headers={"Content-Length": "100"})
    return Response(b"full body", status=200)


@pytest.fixture
def test_zip_data() -> bytes:
    return make_zip(
        {
            "hello.txt": b"Hello from test!",
            "subdir/data.bin": b"\x00\x01\x02\x03",
        }
    )


@pytest.fixture
def zip_server(httpserver, test_zip_data):
    httpserver.expect_request("/test.zip").respond_with_handler(range_handler(test_zip_data))
    return httpserver


@pytest.fixture
def no_range_server(httpserver):
    httpserver.expect_request("/norange.zip").respond_with_handler(no_range_handler)
    return httpserver


@pytest.fixture
def error_server(httpserver):
    httpserver.expect_request("/error.zip").respond_with_response(
        Response(b"not found", status=404)
    )
    return httpserver


@needs_httpx2
class TestHttpx2SyncBackend:
    def test_read_file(self, zip_server, test_zip_data) -> None:
        url = zip_server.url_for("/test.zip")
        reader = Httpx2SyncReader(url)
        with SyncRemoteZip(reader) as rz:
            assert rz.read("hello.txt") == b"Hello from test!"
            assert rz.read("subdir/data.bin") == b"\x00\x01\x02\x03"

    def test_read_into(self, zip_server) -> None:
        url = zip_server.url_for("/test.zip")
        reader = Httpx2SyncReader(url)
        with SyncRemoteZip(reader) as rz:
            dest = io.BytesIO()
            rz.read_into("hello.txt", dest)
            assert dest.getvalue() == b"Hello from test!"

    def test_namelist(self, zip_server) -> None:
        url = zip_server.url_for("/test.zip")
        reader = Httpx2SyncReader(url)
        with SyncRemoteZip(reader) as rz:
            names = rz.namelist()
        assert "hello.txt" in names
        assert "subdir/data.bin" in names

    def test_head(self, zip_server) -> None:
        url = zip_server.url_for("/test.zip")
        reader = Httpx2SyncReader(url)
        headers = reader.head()
        assert headers["accept-ranges"] == "bytes"
        reader.close()

    def test_stream_range(self, zip_server, test_zip_data) -> None:
        url = zip_server.url_for("/test.zip")
        reader = Httpx2SyncReader(url)
        chunks = list(reader.stream_range(0, 10))
        assert b"".join(chunks) == test_zip_data[:10]
        reader.close()

    def test_range_not_supported(self, no_range_server) -> None:
        url = no_range_server.url_for("/norange.zip")
        reader = Httpx2SyncReader(url)
        with pytest.raises(RangeRequestUnsupported):
            reader.head()
        reader.close()

    def test_read_range_not_206(self, no_range_server) -> None:
        url = no_range_server.url_for("/norange.zip")
        reader = Httpx2SyncReader(url)
        with pytest.raises(RangeRequestUnsupported):
            reader.read_range(0, 10)
        reader.close()

    def test_stream_range_not_206(self, no_range_server) -> None:
        url = no_range_server.url_for("/norange.zip")
        reader = Httpx2SyncReader(url)
        with pytest.raises(RangeRequestUnsupported):
            list(reader.stream_range(0, 10))
        reader.close()

    def test_external_client(self, zip_server) -> None:
        url = zip_server.url_for("/test.zip")
        with httpx2.Client() as client:
            reader = Httpx2SyncReader(url, client=client)
            with SyncRemoteZip(reader) as rz:
                assert rz.read("hello.txt") == b"Hello from test!"


@needs_httpx2
class TestHttpx2AsyncBackend:
    async def test_read_file(self, zip_server) -> None:
        url = zip_server.url_for("/test.zip")
        reader = Httpx2AsyncReader(url)
        async with AsyncRemoteZip(reader) as rz:
            assert await rz.read("hello.txt") == b"Hello from test!"
            assert await rz.read("subdir/data.bin") == b"\x00\x01\x02\x03"

    async def test_read_into(self, zip_server) -> None:
        url = zip_server.url_for("/test.zip")
        reader = Httpx2AsyncReader(url)
        async with AsyncRemoteZip(reader) as rz:
            dest = io.BytesIO()
            await rz.read_into("hello.txt", dest)
            assert dest.getvalue() == b"Hello from test!"

    async def test_head(self, zip_server) -> None:
        url = zip_server.url_for("/test.zip")
        reader = Httpx2AsyncReader(url)
        headers = await reader.head()
        assert headers["accept-ranges"] == "bytes"
        await reader.close()

    async def test_stream_range(self, zip_server, test_zip_data) -> None:
        url = zip_server.url_for("/test.zip")
        reader = Httpx2AsyncReader(url)
        chunks = [chunk async for chunk in reader.stream_range(0, 10)]
        assert b"".join(chunks) == test_zip_data[:10]
        await reader.close()

    async def test_range_not_supported(self, no_range_server) -> None:
        url = no_range_server.url_for("/norange.zip")
        reader = Httpx2AsyncReader(url)
        with pytest.raises(RangeRequestUnsupported):
            await reader.head()
        await reader.close()

    async def test_read_range_not_206(self, no_range_server) -> None:
        url = no_range_server.url_for("/norange.zip")
        reader = Httpx2AsyncReader(url)
        with pytest.raises(RangeRequestUnsupported):
            await reader.read_range(0, 10)
        await reader.close()

    async def test_stream_range_not_206(self, no_range_server) -> None:
        url = no_range_server.url_for("/norange.zip")
        reader = Httpx2AsyncReader(url)
        with pytest.raises(RangeRequestUnsupported):
            _ = [chunk async for chunk in reader.stream_range(0, 10)]
        await reader.close()

    async def test_external_client(self, zip_server) -> None:
        url = zip_server.url_for("/test.zip")
        async with httpx2.AsyncClient() as client:
            reader = Httpx2AsyncReader(url, client=client)
            async with AsyncRemoteZip(reader) as rz:
                assert await rz.read("hello.txt") == b"Hello from test!"


class TestUrllib3Backend:
    @pytest.mark.parametrize("status", [200, 206, 403])
    def test_stream_response_cleanup(
        self, httpserver: HTTPServer, monkeypatch: pytest.MonkeyPatch, status: int
    ) -> None:
        data = b"x" * (STREAM_CHUNK_SIZE * 2)
        httpserver.expect_request("/stream.zip").respond_with_data(data, status=status)
        url = httpserver.url_for("/stream.zip")
        with urllib3.PoolManager(maxsize=1, block=True, retries=False, timeout=2) as pool:
            response = pool.request("GET", url, preload_content=False)
            assert isinstance(response, urllib3.response.HTTPResponse)
            connection = response.connection
            assert connection is not None

            def request(*args: object, **kwargs: object) -> urllib3.response.HTTPResponse:
                return response

            with closing(response), monkeypatch.context() as patch:
                patch.setattr(pool, "request", request)
                reader = Urllib3Reader(url, pool=pool)
                with closing(reader.stream_range(0, len(data))) as stream:
                    if status == 206:
                        assert next(stream) == data[:STREAM_CHUNK_SIZE]
                    else:
                        error = OSError if status >= 400 else RangeRequestUnsupported
                        with pytest.raises(error):
                            next(stream)
                assert response.closed
                assert connection.is_closed
            # The external pool must have its slot back after early termination.
            assert pool.request("GET", url, pool_timeout=2).data == data

    def test_read_file(self, zip_server) -> None:
        url = zip_server.url_for("/test.zip")
        reader = Urllib3Reader(url)
        with SyncRemoteZip(reader) as rz:
            assert rz.read("hello.txt") == b"Hello from test!"
            assert rz.read("subdir/data.bin") == b"\x00\x01\x02\x03"

    def test_read_into(self, zip_server) -> None:
        url = zip_server.url_for("/test.zip")
        reader = Urllib3Reader(url)
        with SyncRemoteZip(reader) as rz:
            dest = io.BytesIO()
            rz.read_into("hello.txt", dest)
            assert dest.getvalue() == b"Hello from test!"

    def test_head(self, zip_server) -> None:
        url = zip_server.url_for("/test.zip")
        reader = Urllib3Reader(url)
        headers = reader.head()
        assert headers["accept-ranges"] == "bytes"
        reader.close()

    def test_stream_range(self, zip_server, test_zip_data) -> None:
        url = zip_server.url_for("/test.zip")
        reader = Urllib3Reader(url)
        chunks = list(reader.stream_range(0, 10))
        assert b"".join(chunks) == test_zip_data[:10]
        reader.close()

    def test_range_not_supported(self, no_range_server) -> None:
        url = no_range_server.url_for("/norange.zip")
        reader = Urllib3Reader(url)
        with pytest.raises(RangeRequestUnsupported):
            reader.head()
        reader.close()

    def test_read_range_not_206(self, no_range_server) -> None:
        url = no_range_server.url_for("/norange.zip")
        reader = Urllib3Reader(url)
        with pytest.raises(RangeRequestUnsupported):
            reader.read_range(0, 10)
        reader.close()

    def test_stream_range_not_206(self, no_range_server) -> None:
        url = no_range_server.url_for("/norange.zip")
        reader = Urllib3Reader(url)
        with pytest.raises(RangeRequestUnsupported):
            list(reader.stream_range(0, 10))
        reader.close()

    def test_head_http_error(self, error_server) -> None:
        url = error_server.url_for("/error.zip")
        reader = Urllib3Reader(url)
        with pytest.raises(OSError, match="HEAD request failed"):
            reader.head()
        reader.close()

    def test_read_range_http_error(self, error_server) -> None:
        url = error_server.url_for("/error.zip")
        reader = Urllib3Reader(url)
        with pytest.raises(OSError, match="Range request failed"):
            reader.read_range(0, 10)
        reader.close()

    def test_stream_range_http_error(self, error_server) -> None:
        url = error_server.url_for("/error.zip")
        reader = Urllib3Reader(url)
        with pytest.raises(OSError, match="Range request failed"):
            list(reader.stream_range(0, 10))
        reader.close()

    def test_external_pool(self, zip_server) -> None:
        url = zip_server.url_for("/test.zip")
        pool = urllib3.PoolManager()
        try:
            reader = Urllib3Reader(url, pool=pool)
            with SyncRemoteZip(reader) as rz:
                assert rz.read("hello.txt") == b"Hello from test!"
        finally:
            pool.clear()


@needs_requests
class TestRequestsBackend:
    def test_stream_http_error_closes_response(self, httpserver: HTTPServer) -> None:
        httpserver.expect_request("/error.zip").respond_with_data("forbidden", status=403)
        url = httpserver.url_for("/error.zip")
        with requests.Session() as session:
            reader = RequestsReader(url, session=session)
            with pytest.raises(requests.HTTPError) as error:
                next(reader.stream_range(0, 10))
            response = error.value.response
            assert response is not None
            assert response.raw.closed

    def test_read_file(self, zip_server) -> None:
        url = zip_server.url_for("/test.zip")
        reader = RequestsReader(url)
        with SyncRemoteZip(reader) as rz:
            assert rz.read("hello.txt") == b"Hello from test!"
            assert rz.read("subdir/data.bin") == b"\x00\x01\x02\x03"

    def test_read_into(self, zip_server) -> None:
        url = zip_server.url_for("/test.zip")
        reader = RequestsReader(url)
        with SyncRemoteZip(reader) as rz:
            dest = io.BytesIO()
            rz.read_into("hello.txt", dest)
            assert dest.getvalue() == b"Hello from test!"

    def test_head(self, zip_server) -> None:
        url = zip_server.url_for("/test.zip")
        reader = RequestsReader(url)
        headers = reader.head()
        assert headers["accept-ranges"] == "bytes"
        reader.close()

    def test_stream_range(self, zip_server, test_zip_data) -> None:
        url = zip_server.url_for("/test.zip")
        reader = RequestsReader(url)
        chunks = list(reader.stream_range(0, 10))
        assert b"".join(chunks) == test_zip_data[:10]
        reader.close()

    def test_range_not_supported(self, no_range_server) -> None:
        url = no_range_server.url_for("/norange.zip")
        reader = RequestsReader(url)
        with pytest.raises(RangeRequestUnsupported):
            reader.head()
        reader.close()

    def test_read_range_not_206(self, no_range_server) -> None:
        url = no_range_server.url_for("/norange.zip")
        reader = RequestsReader(url)
        with pytest.raises(RangeRequestUnsupported):
            reader.read_range(0, 10)
        reader.close()

    def test_stream_range_not_206(self, no_range_server) -> None:
        url = no_range_server.url_for("/norange.zip")
        reader = RequestsReader(url)
        with pytest.raises(RangeRequestUnsupported):
            list(reader.stream_range(0, 10))
        reader.close()

    def test_external_session(self, zip_server) -> None:
        url = zip_server.url_for("/test.zip")
        with requests.Session() as session:
            reader = RequestsReader(url, session=session)
            with SyncRemoteZip(reader) as rz:
                assert rz.read("hello.txt") == b"Hello from test!"


@needs_aiohttp
class TestAiohttpBackend:
    async def test_read_file(self, zip_server) -> None:
        url = zip_server.url_for("/test.zip")
        reader = AiohttpReader(url)
        async with AsyncRemoteZip(reader) as rz:
            assert await rz.read("hello.txt") == b"Hello from test!"
            assert await rz.read("subdir/data.bin") == b"\x00\x01\x02\x03"

    async def test_read_into(self, zip_server) -> None:
        url = zip_server.url_for("/test.zip")
        reader = AiohttpReader(url)
        async with AsyncRemoteZip(reader) as rz:
            dest = io.BytesIO()
            await rz.read_into("hello.txt", dest)
            assert dest.getvalue() == b"Hello from test!"

    async def test_head(self, zip_server) -> None:
        url = zip_server.url_for("/test.zip")
        reader = AiohttpReader(url)
        headers = await reader.head()
        assert headers["accept-ranges"] == "bytes"
        await reader.close()

    async def test_stream_range(self, zip_server, test_zip_data) -> None:
        url = zip_server.url_for("/test.zip")
        reader = AiohttpReader(url)
        chunks = [chunk async for chunk in reader.stream_range(0, 10)]
        assert b"".join(chunks) == test_zip_data[:10]
        await reader.close()

    async def test_range_not_supported(self, no_range_server) -> None:
        url = no_range_server.url_for("/norange.zip")
        reader = AiohttpReader(url)
        with pytest.raises(RangeRequestUnsupported):
            await reader.head()
        await reader.close()

    async def test_read_range_not_206(self, no_range_server) -> None:
        url = no_range_server.url_for("/norange.zip")
        reader = AiohttpReader(url)
        with pytest.raises(RangeRequestUnsupported):
            await reader.read_range(0, 10)
        await reader.close()

    async def test_stream_range_not_206(self, no_range_server) -> None:
        url = no_range_server.url_for("/norange.zip")
        reader = AiohttpReader(url)
        with pytest.raises(RangeRequestUnsupported):
            _ = [chunk async for chunk in reader.stream_range(0, 10)]
        await reader.close()

    async def test_external_session(self, zip_server) -> None:
        url = zip_server.url_for("/test.zip")
        async with aiohttp.ClientSession() as session:
            reader = AiohttpReader(url, session=session)
            async with AsyncRemoteZip(reader) as rz:
                assert await rz.read("hello.txt") == b"Hello from test!"


@pytest.fixture
def zip_path(tmp_path, test_zip_data):
    path = tmp_path / "test.zip"
    path.write_bytes(test_zip_data)
    return path


class TestFileBackend:
    def test_is_sync_reader(self, zip_path) -> None:
        assert isinstance(FileReader(zip_path), SyncReader)

    def test_read_file(self, zip_path) -> None:
        reader = FileReader(zip_path)
        with SyncRemoteZip(reader) as rz:
            assert rz.read("hello.txt") == b"Hello from test!"
            assert rz.read("subdir/data.bin") == b"\x00\x01\x02\x03"

    def test_read_into(self, zip_path) -> None:
        reader = FileReader(zip_path)
        with SyncRemoteZip(reader) as rz:
            dest = io.BytesIO()
            rz.read_into("hello.txt", dest)
            assert dest.getvalue() == b"Hello from test!"

    def test_from_uri(self, zip_path) -> None:
        uri = zip_path.as_uri()
        reader = FileReader.from_uri(uri)
        assert reader.url == uri
        with SyncRemoteZip(reader) as rz:
            assert rz.read("hello.txt") == b"Hello from test!"

    def test_url_defaults_to_path(self, zip_path) -> None:
        assert FileReader(zip_path).url == str(zip_path)

    def test_head(self, zip_path, test_zip_data) -> None:
        reader = FileReader(zip_path)
        headers = reader.head()
        assert headers["accept-ranges"] == "bytes"
        assert headers["content-length"] == str(len(test_zip_data))
        reader.close()

    def test_read_range_matches_seek(self, zip_path, test_zip_data) -> None:
        reader = FileReader(zip_path)
        data, headers = reader.read_range(5, 12)
        assert data == test_zip_data[5:17]
        assert headers["content-length"] == str(len(test_zip_data))
        reader.close()

    def test_read_range_past_eof(self, zip_path, test_zip_data) -> None:
        reader = FileReader(zip_path)
        data, _ = reader.read_range(len(test_zip_data) - 3, 100)
        assert data == test_zip_data[-3:]
        reader.close()

    def test_stream_range(self, zip_path, test_zip_data) -> None:
        reader = FileReader(zip_path)
        chunks = list(reader.stream_range(0, 10))
        assert b"".join(chunks) == test_zip_data[:10]
        reader.close()

    def test_context_manager(self, zip_path) -> None:
        with FileReader(zip_path) as reader:
            assert reader.read_range(0, 2)[0] == b"PK"
        assert reader._handle is None

    def test_missing_path(self, tmp_path) -> None:
        reader = FileReader(tmp_path / "does-not-exist.zip")
        with pytest.raises(FileNotFoundError):
            reader.head()

    def test_from_uri_not_file_scheme(self) -> None:
        with pytest.raises(ValueError, match="file://"):
            FileReader.from_uri("https://example.com/data.zip")

    def test_from_uri_remote_host(self) -> None:
        with pytest.raises(ValueError, match="host"):
            FileReader.from_uri("file://remotehost/path/data.zip")

    def test_from_uri_localhost(self, zip_path) -> None:
        uri = f"file://localhost{zip_path.as_uri()[len('file://') :]}"
        reader = FileReader.from_uri(uri)
        with SyncRemoteZip(reader) as rz:
            assert rz.read("hello.txt") == b"Hello from test!"


class TestAsyncFileBackend:
    def test_cancel_on_loop_shutdown(
        self, zip_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reader = AsyncFileReader(zip_path)
        release = threading.Event()
        with zip_path.open("rb") as handle:

            async def main() -> None:
                loop = asyncio.get_running_loop()
                started = asyncio.Event()

                def delayed_open(path: str, mode: str) -> typing.BinaryIO:
                    loop.call_soon_threadsafe(started.set)
                    assert release.wait(5), "Open worker was not released"
                    return handle

                monkeypatch.setattr("zipwire.backends._file.open", delayed_open, raising=False)

                async def use_reader() -> None:
                    async with reader:
                        await reader.head()

                task = asyncio.create_task(use_reader())
                await asyncio.wait_for(started.wait(), 5)
                assert not task.done()
                # Returning with pending work makes asyncio.run cancel all
                # tasks, not just the reader's caller. Release the worker
                # while shutdown waits for cancellation and cleanup.
                loop.call_later(0.1, release.set)

            try:
                asyncio.run(main())
                assert handle.closed
                assert reader._handle is None
                assert reader._size is None
            finally:
                release.set()

    @pytest.mark.parametrize("operation", ["head", "read_range", "stream_range"])
    @pytest.mark.parametrize("fail", [False, True])
    async def test_cancel_during_open(
        self, zip_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str, fail: bool
    ) -> None:
        reader = AsyncFileReader(zip_path)
        loop = asyncio.get_running_loop()
        started = asyncio.Event()
        release = threading.Event()

        with zip_path.open("rb") as handle:

            def delayed_open(path: str, mode: str) -> typing.BinaryIO:
                loop.call_soon_threadsafe(started.set)
                assert release.wait(5), "Open worker was not released"
                if fail:
                    raise OSError("Open failed after cancellation")
                return handle

            monkeypatch.setattr("zipwire.backends._file.open", delayed_open, raising=False)

            async def use_reader() -> None:
                async with reader:
                    if operation == "head":
                        await reader.head()
                    elif operation == "read_range":
                        await reader.read_range(0, 2)
                    else:
                        async for _ in reader.stream_range(0, 2):
                            pass

            task = asyncio.create_task(use_reader())
            try:
                await asyncio.wait_for(started.wait(), 5)
                # Neither the first nor a repeated cancellation may let
                # cleanup finish while the open is still running.
                for _ in range(2):
                    task.cancel()
                    await asyncio.sleep(0)
                    assert not task.done()
            finally:
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 5)
            assert reader._handle is None
            assert reader._size is None
            if not fail:
                assert handle.closed

    async def test_cancel_during_close(
        self, zip_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reader = AsyncFileReader(zip_path)
        await reader.head()
        handle = reader._handle
        assert handle is not None
        close = handle.close
        loop = asyncio.get_running_loop()
        started = asyncio.Event()
        release = threading.Event()

        def delayed_close() -> None:
            loop.call_soon_threadsafe(started.set)
            assert release.wait(5), "Close worker was not released"
            close()

        monkeypatch.setattr(handle, "close", delayed_close)
        task = asyncio.create_task(reader.close())
        try:
            await asyncio.wait_for(started.wait(), 5)
            for _ in range(2):
                task.cancel()
                await asyncio.sleep(0)
                assert not task.done()
        finally:
            release.set()
            try:
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 5)
                assert handle.closed
            finally:
                close()
        assert reader._handle is None
        assert reader._size is None

    def test_is_async_reader(self, zip_path) -> None:
        assert isinstance(AsyncFileReader(zip_path), AsyncReader)

    async def test_read_file(self, zip_path) -> None:
        reader = AsyncFileReader(zip_path)
        async with AsyncRemoteZip(reader) as rz:
            assert await rz.read("hello.txt") == b"Hello from test!"
            assert await rz.read("subdir/data.bin") == b"\x00\x01\x02\x03"

    async def test_read_into(self, zip_path) -> None:
        reader = AsyncFileReader(zip_path)
        async with AsyncRemoteZip(reader) as rz:
            dest = io.BytesIO()
            await rz.read_into("hello.txt", dest)
            assert dest.getvalue() == b"Hello from test!"

    async def test_from_uri(self, zip_path) -> None:
        uri = zip_path.as_uri()
        reader = AsyncFileReader.from_uri(uri)
        assert reader.url == uri
        async with AsyncRemoteZip(reader) as rz:
            assert await rz.read("hello.txt") == b"Hello from test!"

    async def test_head(self, zip_path, test_zip_data) -> None:
        reader = AsyncFileReader(zip_path)
        headers = await reader.head()
        assert headers["accept-ranges"] == "bytes"
        assert headers["content-length"] == str(len(test_zip_data))
        await reader.close()

    async def test_read_range_matches_seek(self, zip_path, test_zip_data) -> None:
        reader = AsyncFileReader(zip_path)
        data, headers = await reader.read_range(5, 12)
        assert data == test_zip_data[5:17]
        assert headers["content-length"] == str(len(test_zip_data))
        await reader.close()

    async def test_stream_range(self, zip_path, test_zip_data) -> None:
        reader = AsyncFileReader(zip_path)
        chunks = [chunk async for chunk in reader.stream_range(0, 10)]
        assert b"".join(chunks) == test_zip_data[:10]
        await reader.close()

    async def test_context_manager(self, zip_path) -> None:
        async with AsyncFileReader(zip_path) as reader:
            data, _ = await reader.read_range(0, 2)
            assert data == b"PK"
        assert reader._handle is None

    async def test_missing_path(self, tmp_path) -> None:
        reader = AsyncFileReader(tmp_path / "does-not-exist.zip")
        with pytest.raises(FileNotFoundError):
            await reader.head()

    def test_from_uri_not_file_scheme(self) -> None:
        with pytest.raises(ValueError, match="file://"):
            AsyncFileReader.from_uri("https://example.com/data.zip")


class TestBackendsInit:
    def test_unknown_attribute(self) -> None:
        with pytest.raises(AttributeError, match="NonExistentReader"):
            getattr(backends, "NonExistentReader")  # noqa: B009

    def test_dir(self) -> None:
        names = dir(backends)
        assert "Httpx2SyncReader" in names
        assert "Httpx2AsyncReader" in names
        assert "AiohttpReader" in names
        assert "Urllib3Reader" in names
        assert "RequestsReader" in names
        assert "FileReader" in names
        assert "AsyncFileReader" in names

    @pytest.mark.skipif(has_httpx2, reason="httpx2 is installed")
    def test_httpx2_import_error(self) -> None:
        with pytest.raises(ImportError, match="httpx2"):
            backends.Httpx2SyncReader  # noqa: B018

    @pytest.mark.skipif(has_requests, reason="requests is installed")
    def test_requests_import_error(self) -> None:
        with pytest.raises(ImportError, match="requests"):
            backends.RequestsReader  # noqa: B018

    @pytest.mark.skipif(has_aiohttp, reason="aiohttp is installed")
    def test_aiohttp_import_error(self) -> None:
        with pytest.raises(ImportError, match="aiohttp"):
            backends.AiohttpReader  # noqa: B018
