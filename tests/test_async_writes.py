"""Async destinations, backpressure, and cleanup during ZIP extraction."""

from __future__ import annotations

import asyncio
import typing
import zipfile
from random import Random

import pytest

from tests.conftest import MockAsyncReader, has_httpx2, make_zip, needs_httpx2, needs_zstd
from zipwire import AsyncRemoteZip, AsyncWritable, CRCMismatch, FileTooLarge

if has_httpx2:
    import anyio

if typing.TYPE_CHECKING:
    from collections.abc import AsyncIterator
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

    async def stream_range(self, offset: int, length: int) -> AsyncIterator[bytes]:
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
async def test_actual_output_limit(size: int) -> None:
    """Check actual output when the central directory understates its size."""
    reader = TrackingReader(make_zip({"entry.bin": b"x" * size}))
    destination = AsyncDestination()
    async with AsyncRemoteZip(reader) as archive:
        info = archive.getinfo("entry.bin")
        info.file_size = 1
        with pytest.raises(FileTooLarge):
            await archive.read_into(info, destination, max_file_size=50)
        assert not destination.data
        if size == 70_000:
            assert reader.stream_closed


async def test_iterator_without_aclose(large_file_zip: bytes) -> None:
    """Keep custom backends that return a plain async iterator compatible."""

    class PlainIterator:
        def __init__(self, stream: AsyncIterator[bytes]) -> None:
            self.stream = stream

        def __aiter__(self) -> PlainIterator:
            return self

        async def __anext__(self) -> bytes:
            return await anext(self.stream)

    class PlainReader(MockAsyncReader):
        def stream_range(self, offset: int, length: int) -> AsyncIterator[bytes]:
            return PlainIterator(super().stream_range(offset, length))

    destination = AsyncDestination()
    async with AsyncRemoteZip(PlainReader(large_file_zip)) as archive:
        await archive.read_into("large.bin", destination)
    assert destination.data == bytes(range(256)) * 208


@needs_httpx2
async def test_anyio_file(stored_zip: bytes, tmp_path: Path) -> None:
    """Exercise a real async file writer without a new runtime dependency."""
    path = tmp_path / "output.txt"
    async with AsyncRemoteZip(MockAsyncReader(stored_zip)) as archive:
        async with await anyio.open_file(path, "wb") as destination:
            await archive.read_into("hello.txt", destination)
            assert not destination.closed
            await destination.write(b"!")
        assert await anyio.Path(path).read_bytes() == b"Hello, World!!"
