"""Range reuse, early rejection, and cooperative async decoding."""

from __future__ import annotations

import asyncio
import io
import struct
import typing
import zipfile
from contextlib import AsyncExitStack
from random import Random
from zlib import crc32

import pytest

from tests.conftest import MockAsyncReader, MockSyncReader, make_zip
from tests.test_parity import Unseekable, opened, read, zip64
from tests.test_parity import kind as kind
from zipwire import (
    AsyncRemoteWheel,
    AsyncRemoteZip,
    BadZipFile,
    FileTooLarge,
    SyncRemoteWheel,
    SyncRemoteZip,
)
from zipwire._constants import STREAM_CHUNK_SIZE

if typing.TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator


@pytest.mark.parametrize("empty", [False, True])
async def test_directory_reuses_tail(kind: str, empty: bool) -> None:
    data = make_zip({} if empty else {"file": b"payload"})
    async with opened(data, kind) as archive:
        reader = archive._reader
        assert isinstance(reader, (MockSyncReader, MockAsyncReader))
        assert archive.namelist() == ([] if empty else ["file"])
        assert reader.read_count == 1


async def test_directory_outside_tail(kind: str) -> None:
    files = {f"{number:04d}" + "x" * 100: b"" for number in range(2000)}
    async with opened(make_zip(files), kind) as archive:
        reader = archive._reader
        assert isinstance(reader, (MockSyncReader, MockAsyncReader))
        assert archive.namelist() == list(files)
        assert reader.read_count == 2


async def test_empty_zip64_directory_outside_tail(kind: str) -> None:
    data = zip64(make_zip({}), extension=b"\0" * 140000, comment=b"c" * 65535)
    async with opened(data, kind) as archive:
        reader = archive._reader
        assert isinstance(reader, (MockSyncReader, MockAsyncReader))
        assert archive.namelist() == []
        # The tail and ZIP64 footer suffice; never request an empty range.
        assert reader.read_count == 2


async def test_short_tail_is_rejected(kind: str) -> None:
    data = make_zip({"file": b"payload"})

    class Reader(MockSyncReader):
        def read_range(self, offset: int, length: int) -> tuple[bytes, dict[str, str]]:
            body, headers = super().read_range(offset, length)
            return body[:-1], headers

    class AsyncReader(MockAsyncReader):
        async def read_range(self, offset: int, length: int) -> tuple[bytes, dict[str, str]]:
            body, headers = await super().read_range(offset, length)
            return body[:-1], headers

    url = "https://example.com/demo-1.0-py3-none-any.whl"
    if kind.startswith("async"):
        reader = AsyncReader(data, url=url)
        cls = AsyncRemoteWheel if kind.endswith("wheel") else AsyncRemoteZip
        with pytest.raises(BadZipFile, match="tail is truncated"):
            async with cls(reader):
                pass
    else:
        reader = Reader(data, url=url)
        cls = SyncRemoteWheel if kind.endswith("wheel") else SyncRemoteZip
        with pytest.raises(BadZipFile, match="tail is truncated"), cls(reader):
            pass
    assert reader.closed
    assert reader.read_count == 1


@pytest.mark.parametrize("into", [False, True])
@pytest.mark.parametrize(
    "case",
    [
        "crc_collision",
        "empty_deflate",
        "stored_zero",
        "declared_size",
        "compressed_declared_size",
        "codec_eof",
    ],
)
async def test_short_member_response(kind: str, into: bool, case: str) -> None:
    payload = b"payload"
    if case == "crc_collision":
        # Appending a CRC produces a fixed residue, so the final four
        # bytes can be lost without changing the CRC of the prefix.
        payload += crc32(payload).to_bytes(4, "little")
        payload += crc32(payload).to_bytes(4, "little")
        assert crc32(payload) == crc32(payload[:-4])
    elif case == "empty_deflate":
        payload = b""
    info = zipfile.ZipInfo("file")
    info.compress_type = (
        zipfile.ZIP_DEFLATED
        if case in ("empty_deflate", "compressed_declared_size", "codec_eof")
        else zipfile.ZIP_STORED
    )
    # A long header prevents prefetching the compressed payload.
    info.extra = struct.pack("<HH", 0xCAFE, 2048) + b"x" * 2048
    buffer = Unseekable()
    with zipfile.ZipFile(buffer, "w") as writer:
        writer.writestr(info, payload)
        writer.writestr("padding", b"x" * 200000)
    data = bytearray(buffer.getvalue())
    with zipfile.ZipFile(io.BytesIO(data)) as reference:
        cd = reference.start_dir
    data_start = 30 + len(info.filename) + len(info.extra)
    if case == "crc_collision":
        available = info.compress_size - 4
    elif case in ("empty_deflate", "stored_zero"):
        available = 0
        if case == "stored_zero":
            struct.pack_into("<I", data, cd + 24, 0)
            struct.pack_into("<I", data, cd + 16, 0)
    elif case in ("declared_size", "compressed_declared_size"):
        available = 4 if case == "compressed_declared_size" else 3
        struct.pack_into("<I", data, cd + 24, 3)
        struct.pack_into("<I", data, cd + 16, crc32(payload[:3]))
    else:
        available = info.compress_size
        # Include part of the data descriptor in the compressed extent.
        # The codec's EOF still terminates the entry before those bytes.
        struct.pack_into("<I", data, cd + 20, info.compress_size + 8)
        struct.pack_into("<I", data, cd + 24, len(payload) + 1)
    with zipfile.ZipFile(io.BytesIO(data)) as reference:
        compressed_end = data_start + reference.getinfo("file").compress_size
    cutoff = data_start + available
    response = bytes(data[data_start:cutoff])

    class ShortFile(io.BytesIO):
        def read(self, size: int | None = -1, /) -> bytes:
            if data_start <= self.tell() < compressed_end:
                remaining = max(0, cutoff - self.tell())
                size = remaining if size is None or size < 0 else min(size, remaining)
            return super().read(size)

    class Reader(MockSyncReader):
        stream_closed = False

        def read_range(self, offset: int, length: int) -> tuple[bytes, dict[str, str]]:
            body, headers = super().read_range(offset, length)
            return (response if offset == data_start else body), headers

        def stream_range(self, offset: int, length: int) -> Iterator[bytes]:
            try:
                if response:
                    yield response
            finally:
                self.stream_closed = True

    class AsyncReader(MockAsyncReader):
        stream_closed = False

        async def read_range(self, offset: int, length: int) -> tuple[bytes, dict[str, str]]:
            body, headers = await super().read_range(offset, length)
            return (response if offset == data_start else body), headers

        async def stream_range(self, offset: int, length: int) -> AsyncIterator[bytes]:
            try:
                if response:
                    yield response
            finally:
                self.stream_closed = True

    with zipfile.ZipFile(ShortFile(data)) as reference:
        try:
            expected = reference.read("file")
        except EOFError:
            expected = None
    assert (expected is None) == (case in ("crc_collision", "empty_deflate", "stored_zero"))
    url = "https://example.com/demo-1.0-py3-none-any.whl"
    async with AsyncExitStack() as stack:
        if kind.startswith("async"):
            reader = AsyncReader(bytes(data), url=url)
            cls = AsyncRemoteWheel if kind.endswith("wheel") else AsyncRemoteZip
            archive = await stack.enter_async_context(cls(reader))
        else:
            reader = Reader(bytes(data), url=url)
            cls = SyncRemoteWheel if kind.endswith("wheel") else SyncRemoteZip
            archive = stack.enter_context(cls(reader))
        if expected is None:
            with pytest.raises(EOFError):
                await read(archive, "file", into=into)
        else:
            assert await read(archive, "file", into=into) == expected
        if into:
            assert reader.stream_closed
    assert reader.closed


@pytest.mark.parametrize("into", [False, True])
async def test_size_limit_before_member_request(kind: str, into: bool) -> None:
    async with opened(make_zip({"file": b"payload"}), kind) as archive:
        reader = archive._reader
        assert isinstance(reader, (MockSyncReader, MockAsyncReader))
        before = reader.read_count
        with pytest.raises(FileTooLarge):
            await read(archive, "file", into=into, max_file_size=0)
        assert reader.read_count == before


async def test_stream_closed_at_declared_size(kind: str) -> None:
    data = bytearray(make_zip({"file": b"payload" * 10000, "padding": b"x" * 200000}))
    with zipfile.ZipFile(io.BytesIO(data)) as reference:
        cd = reference.start_dir
    struct.pack_into("<I", data, cd + 24, 7)
    # The original CRC covers the full payload; copy a short entry's CRC.
    with zipfile.ZipFile(io.BytesIO(make_zip({"file": b"payload"}))) as reference:
        struct.pack_into("<I", data, cd + 16, reference.getinfo("file").CRC)
    closed = False

    class Reader(MockSyncReader):
        def stream_range(self, offset: int, length: int) -> Iterator[bytes]:
            nonlocal closed
            try:
                yield self.data[offset : offset + 1024]
                pytest.fail("Read past the declared output size")
            finally:
                closed = True

    class AsyncReader(MockAsyncReader):
        async def stream_range(self, offset: int, length: int) -> AsyncIterator[bytes]:
            nonlocal closed
            try:
                yield self.data[offset : offset + 1024]
                pytest.fail("Read past the declared output size")
            finally:
                closed = True

    url = "https://example.com/demo-1.0-py3-none-any.whl"
    if kind.startswith("async"):
        cls = AsyncRemoteWheel if kind.endswith("wheel") else AsyncRemoteZip
        async with cls(AsyncReader(bytes(data), url=url)) as archive:
            assert await read(archive, "file", into=True) == b"payload"
            assert closed
    else:
        cls = SyncRemoteWheel if kind.endswith("wheel") else SyncRemoteZip
        with cls(Reader(bytes(data), url=url)) as archive:
            assert await read(archive, "file", into=True) == b"payload"
            assert closed


@pytest.mark.parametrize("kind", ["async", "async_wheel"])
@pytest.mark.parametrize("into", [False, True])
async def test_cancel_while_discarding_output(kind: str, into: bool) -> None:
    data = bytearray(
        make_zip({"file": b"x" * (STREAM_CHUNK_SIZE * 8)}, compression=zipfile.ZIP_DEFLATED)
    )
    with zipfile.ZipFile(io.BytesIO(data)) as reference:
        cd = reference.start_dir
    struct.pack_into("<I", data, cd + 24, 0)
    struct.pack_into("<I", data, cd + 16, 0)
    async with opened(bytes(data), kind) as archive:
        task = asyncio.create_task(read(archive, "file", into=into))
        try:
            # Validation must continue and yield even when every output
            # chunk is discarded instead of being passed to a destination.
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert not task.done()
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


@pytest.mark.parametrize("kind", ["sync_wheel", "async_wheel"])
@pytest.mark.parametrize("long_header", [False, True])
async def test_cached_member_bounds(kind: str, long_header: bool) -> None:
    name = "x" * 5000 if long_header else "file"
    info = zipfile.ZipInfo(name)
    if long_header:
        info.extra = struct.pack("<HH", 0xCAFE, 5000) + b"\0" * 5000
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as writer:
        writer.writestr("prefix", b"prefix")
        writer.writestr(info, b"payload")
    async with opened(buffer.getvalue(), kind) as archive:
        reader = archive._reader
        assert isinstance(reader, (MockSyncReader, MockAsyncReader))
        before = reader.read_count
        assert await read(archive, name) == b"payload"
        assert await read(archive, name, into=True) == b"payload"
        assert reader.read_count == before


@pytest.mark.parametrize("kind", ["sync_wheel", "async_wheel"])
async def test_member_outside_tail(kind: str) -> None:
    data = make_zip({"file": b"payload", "padding": b"x" * (256 * 1024)})
    async with opened(data, kind) as archive:
        reader = archive._reader
        assert isinstance(reader, (MockSyncReader, MockAsyncReader))
        before = reader.read_count
        assert await read(archive, "file") == b"payload"
        assert reader.read_count == before + 1


@pytest.mark.parametrize("kind", ["async", "async_wheel"])
@pytest.mark.parametrize(
    "method", [zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED, zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA]
)
async def test_async_read_yields(kind: str, method: int) -> None:
    payload = b"x" * (4 * 1024**2)
    async with opened(make_zip({"file": payload}, compression=method), kind) as archive:
        other_work_done = False

        def other_work() -> None:
            nonlocal other_work_done
            other_work_done = True

        asyncio.get_running_loop().call_soon(other_work)
        assert await read(archive, "file") == payload
        assert other_work_done


@pytest.mark.parametrize("cached", [False, True])
@pytest.mark.parametrize("short_write", [False, True])
async def test_cancel_during_decoding(cached: bool, short_write: bool) -> None:
    payload = Random(0).randbytes(70000) + b"x" * (6 * 1024**2)
    data = make_zip({"file": payload}, compression=zipfile.ZIP_DEFLATED)

    class Reader(MockAsyncReader):
        stream_closed = False

        async def stream_range(self, offset: int, length: int) -> AsyncIterator[bytes]:
            try:
                yield self.data[offset : offset + length]
            finally:
                self.stream_closed = True

    class Destination:
        size = 0

        def write(self, chunk: bytes) -> int:
            if not self.size:
                asyncio.get_running_loop().call_soon(task.cancel)
            size = min(3, len(chunk)) if short_write else len(chunk)
            self.size += size
            return size

    reader = Reader(data, url="https://example.com/demo-1.0-py3-none-any.whl")
    cls = AsyncRemoteWheel if cached else AsyncRemoteZip
    async with cls(reader) as archive:
        dest = Destination()
        task = asyncio.create_task(archive.read_into("file", dest))
        with pytest.raises(asyncio.CancelledError):
            await task
        assert 0 < dest.size < len(payload)
        if short_write:
            assert dest.size == 3
        if not cached:
            assert reader.stream_closed
        assert not reader.closed
    assert reader.closed
