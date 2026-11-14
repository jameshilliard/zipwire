"""Async destinations, backpressure, and cleanup during ZIP extraction."""

from __future__ import annotations

import asyncio
import io
import typing
import zipfile
from random import Random
from zlib import crc32

import anyio
import pytest

from tests.conftest import (
    MockAsyncReader,
    make_zip,
    needs_anyio,
    needs_zstd,
)
from zipwire import AsyncRemoteZip, AsyncWritable, CRCMismatch, FileTooLarge
from zipwire._write import awrite_all

if typing.TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path


class AsyncDestination:
    """Record writes only after their coroutine has been awaited."""

    def __init__(self) -> None:
        self.data = bytearray()
        self.closed = False
        self.flushed = False

    async def write(self, data: bytes) -> int:
        await asyncio.sleep(0)
        self.data.extend(data)
        return len(data)

    async def flush(self) -> None:
        self.flushed = True

    async def close(self) -> None:
        self.closed = True


class TrackingReader(MockAsyncReader):
    """Observe input consumption and asynchronous response cleanup."""

    def __init__(self, data: bytes) -> None:
        super().__init__(data)
        self.chunks_read = 0
        self.stream_closed = False

    async def stream_range(self, offset: int, length: int) -> AsyncGenerator[bytes, None]:
        try:
            async for chunk in super().stream_range(offset, length):
                self.chunks_read += 1
                yield chunk
        finally:
            await asyncio.sleep(0)
            self.stream_closed = True


@pytest.mark.parametrize(
    "compression",
    [
        zipfile.ZIP_STORED,
        zipfile.ZIP_DEFLATED,
        zipfile.ZIP_BZIP2,
        zipfile.ZIP_LZMA,
        pytest.param(getattr(zipfile, "ZIP_ZSTANDARD", 93), marks=needs_zstd),
    ],
)
@pytest.mark.parametrize("size", [0, 100, 70_000])
async def test_async_destination(compression: int, size: int) -> None:
    """Await writes for empty, prefetched, and streamed entries."""
    content = Random(0).randbytes(size)
    reader = TrackingReader(make_zip({"entry.bin": content}, compression=compression))
    destination = AsyncDestination()
    typed_destination: AsyncWritable = destination
    async with AsyncRemoteZip(reader) as archive:
        await archive.read_into("entry.bin", typed_destination)
    assert destination.data == content
    assert not destination.closed
    assert not destination.flushed
    if size == 70_000:
        assert reader.chunks_read > 1
        assert reader.stream_closed


async def test_writer_returning_future(stored_zip: bytes) -> None:
    """Await the write result even when write is not a coroutine function."""
    data = bytearray()

    class FutureDestination:
        def write(self, chunk: bytes) -> asyncio.Future[None]:
            loop = asyncio.get_running_loop()
            done: asyncio.Future[None] = loop.create_future()

            def complete() -> None:
                data.extend(chunk)
                done.set_result(None)

            loop.call_soon(complete)
            return done

    async with AsyncRemoteZip(MockAsyncReader(stored_zip)) as archive:
        await archive.read_into("hello.txt", FutureDestination())
        assert data == b"Hello, World!"


async def test_writer_returning_non_awaitable(stored_zip: bytes) -> None:
    """Accept sync writers whose write() returns something other than an int."""

    class NoneDestination:
        def __init__(self) -> None:
            self.data = bytearray()

        def write(self, chunk: bytes) -> None:
            self.data.extend(chunk)

    destination = NoneDestination()
    async with AsyncRemoteZip(MockAsyncReader(stored_zip)) as archive:
        await archive.read_into("hello.txt", destination)
    assert destination.data == b"Hello, World!"


@pytest.mark.parametrize("size", [100, 70_000])
async def test_writer_returning_awaitable_int(size: int) -> None:
    """Await int subclasses instead of treating their value as a completed write."""
    content = Random(0).randbytes(size)
    awaited_bytes = 0

    class AwaitableCount(int):
        def __await__(self):
            async def complete() -> int:
                nonlocal awaited_bytes
                await asyncio.sleep(0)
                awaited_bytes += int(self)
                return int(self)

            return complete().__await__()

    class Destination:
        def write(self, chunk: bytes) -> AwaitableCount:
            return AwaitableCount(len(chunk))

    async with AsyncRemoteZip(MockAsyncReader(make_zip({"file": content}))) as archive:
        await archive.read_into("file", Destination())
    assert awaited_bytes == len(content)


def test_awrite_all_is_loop_agnostic() -> None:
    """awrite_all must not use event-loop primitives such as asyncio.sleep(0).

    A checkpoint like asyncio.sleep(0) passes under asyncio but raises
    RuntimeError on other event loops such as trio.  Driving the coroutine
    by hand, with no event loop at all, detects any await that needs one:
    the coroutine must run to completion without ever suspending.
    """
    chunks = [b"Hello, ", b"World!"]

    class SyncWriter:
        def __init__(self) -> None:
            self.data = bytearray()

        def write(self, chunk: bytes) -> int:
            self.data.extend(chunk)
            return len(chunk)

    class AsyncShortWriter:
        """Accept at most five bytes per call to exercise the retry loop."""

        def __init__(self) -> None:
            self.data = bytearray()

        async def write(self, chunk: bytes) -> int:
            count = min(5, len(chunk))
            self.data.extend(chunk[:count])
            return count

    for destination in (SyncWriter(), AsyncShortWriter()):
        coroutine = awrite_all(destination, chunks)
        try:
            coroutine.send(None)
        except StopIteration:
            pass
        else:
            coroutine.close()
            pytest.fail("awrite_all suspended on an event-loop primitive")
        assert destination.data == b"Hello, World!"


@pytest.mark.parametrize("size", [100, 70_000])
@pytest.mark.parametrize("write_limit", [None, 3])
async def test_async_partial_writes(size: int, write_limit: int | None) -> None:
    """Retry the unconsumed suffix after awaiting each partial write."""
    content = Random(0).randbytes(size)
    output = bytearray()

    class Destination:
        async def write(self, chunk: bytes) -> int | None:
            await asyncio.sleep(0)
            count = len(chunk) if write_limit is None else min(write_limit, len(chunk))
            output.extend(chunk[:count])
            return None if write_limit is None else count

    reader = TrackingReader(make_zip({"file": content}))
    async with AsyncRemoteZip(reader) as archive:
        await archive.read_into("file", Destination())
    assert output == content
    if size == 70_000:
        assert reader.stream_closed


@pytest.mark.parametrize("size", [100, 70_000])
@pytest.mark.parametrize("failure", ["zero", "negative", "oversized", "noninteger", "boolean"])
async def test_async_invalid_write_count(size: int, failure: str) -> None:
    """Validate resolved counts and release streamed input on failure."""

    class Destination:
        async def write(self, chunk: bytes) -> int:
            await asyncio.sleep(0)
            if failure == "zero":
                return 0
            if failure == "negative":
                return -1
            if failure == "oversized":
                return len(chunk) + 1
            if failure == "boolean":
                # bool is an int subclass; a success flag is not a count.
                return typing.cast("int", True)
            return typing.cast("int", "invalid")

    reader = TrackingReader(make_zip({"file": b"x" * size}))
    async with AsyncRemoteZip(reader) as archive:
        with pytest.raises(OSError, match="invalid byte count"):
            await archive.read_into("file", Destination())
        if size == 70_000:
            assert reader.stream_closed


def test_async_writable_is_not_runtime_checkable() -> None:
    """isinstance() cannot tell sync and async writers apart, so it must fail."""
    with pytest.raises(TypeError, match="runtime_checkable"):
        isinstance(io.BytesIO(), AsyncWritable)  # type: ignore[misc]


async def test_writer_backpressure(large_file_zip: bytes) -> None:
    """A paused write must prevent the next compressed-input read."""
    started = asyncio.Event()
    resume = asyncio.Event()

    class PausedDestination(AsyncDestination):
        async def write(self, data: bytes) -> int:
            started.set()
            await resume.wait()
            return await super().write(data)

    reader = TrackingReader(large_file_zip)
    destination = PausedDestination()
    async with AsyncRemoteZip(reader) as archive, asyncio.TaskGroup() as tasks:
        tasks.create_task(archive.read_into("large.bin", destination))
        await asyncio.wait_for(started.wait(), timeout=5)
        await asyncio.sleep(0)
        assert reader.chunks_read == 1
        assert not destination.data
        resume.set()
    assert destination.data == bytes(range(256)) * 208
    assert reader.stream_closed


@pytest.mark.parametrize("size", [100, 70_000])
async def test_writer_error_closes_input(size: int) -> None:
    """Write failures propagate and streamed input closes before returning."""
    reader = TrackingReader(make_zip({"entry.bin": b"x" * size}))

    class FailingDestination(AsyncDestination):
        async def write(self, data: bytes) -> int:
            await asyncio.sleep(0)
            raise OSError("write failed")

    destination = FailingDestination()
    async with AsyncRemoteZip(reader) as archive:
        with pytest.raises(OSError, match="write failed"):
            await archive.read_into("entry.bin", destination)
        if size == 70_000:
            assert reader.stream_closed
        assert not reader.closed
        assert not destination.closed
        assert not destination.flushed


async def test_cancellation_closes_input(large_file_zip: bytes) -> None:
    """Cancellation during an awaited write releases the input response."""
    started = asyncio.Event()

    class PausedDestination(AsyncDestination):
        async def write(self, data: bytes) -> int:
            started.set()
            await asyncio.Event().wait()
            return await super().write(data)

    reader = TrackingReader(large_file_zip)
    destination = PausedDestination()
    async with AsyncRemoteZip(reader) as archive, asyncio.TaskGroup() as tasks:
        task = tasks.create_task(archive.read_into("large.bin", destination))
        await asyncio.wait_for(started.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert reader.stream_closed
        assert reader.chunks_read == 1
        assert not destination.closed


async def test_crc_failure_propagates(stored_zip: bytes) -> None:
    """Successfully writing all chunks does not bypass final verification."""
    destination = AsyncDestination()
    async with AsyncRemoteZip(MockAsyncReader(stored_zip)) as archive:
        info = archive.getinfo("hello.txt")
        info.CRC ^= 1
        with pytest.raises(CRCMismatch):
            await archive.read_into(info, destination)
    assert destination.data == b"Hello, World!"
    assert not destination.closed


@pytest.mark.parametrize("size", [100, 70_000])
async def test_declared_size_caps_async_output(size: int) -> None:
    """Truncate to the declared size and verify the returned bytes' CRC."""
    reader = TrackingReader(make_zip({"entry.bin": b"x" * size}))
    destination = AsyncDestination()
    async with AsyncRemoteZip(reader) as archive:
        info = archive.getinfo("entry.bin")
        info.file_size = 1
        info.CRC = crc32(b"x")
        await archive.read_into(info, destination, max_file_size=50)
        assert destination.data == b"x"
        if size == 70_000:
            assert reader.stream_closed


@pytest.mark.parametrize("size", [100, 70_000])
async def test_async_destination_size_limit(size: int) -> None:
    """Reject oversized entries before fetching member data or writing."""
    reader = TrackingReader(make_zip({"entry.bin": b"x" * size}))
    destination = AsyncDestination()
    async with AsyncRemoteZip(reader) as archive:
        read_count = reader.read_count
        with pytest.raises(FileTooLarge):
            await archive.read_into("entry.bin", destination, max_file_size=50)
        assert reader.read_count == read_count
        assert reader.chunks_read == 0
        assert not destination.data


@needs_anyio
async def test_anyio_file(stored_zip: bytes, tmp_path: Path) -> None:
    """Exercise a real async file writer."""
    assert anyio is not None
    path = tmp_path / "output.txt"
    async with AsyncRemoteZip(MockAsyncReader(stored_zip)) as archive:
        async with await anyio.open_file(path, "wb") as destination:
            await archive.read_into("hello.txt", destination)
            assert not destination.closed
            await destination.write(b"!")
        assert await anyio.Path(path).read_bytes() == b"Hello, World!!"
