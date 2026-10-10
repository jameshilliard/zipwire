"""Integration tests for backends using pytest-httpserver."""

from __future__ import annotations

import io

import pytest
import urllib3
from werkzeug import Request, Response

from tests.conftest import (
    has_aiobotocore,
    has_aiohttp,
    has_httpx2,
    has_requests,
    make_zip,
    needs_aiohttp,
    needs_httpx2,
    needs_requests,
)
from zipwire import AsyncReader, AsyncRemoteZip, SyncReader, SyncRemoteZip, backends
from zipwire._errors import RangeRequestUnsupported
from zipwire.backends import AsyncFileReader, FileReader, Urllib3Reader

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
        assert "AiobotocoreReader" in names
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

    @pytest.mark.skipif(has_aiobotocore, reason="aiobotocore is installed")
    def test_aiobotocore_import_error(self) -> None:
        with pytest.raises(ImportError, match="aiobotocore"):
            backends.AiobotocoreReader  # noqa: B018
