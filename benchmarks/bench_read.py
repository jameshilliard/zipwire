"""Measure local extraction overhead, without network or archive creation time.

Run with the project environment: .venv/bin/python benchmarks/bench_read.py
An optional --encrypted-zip supplies a single-entry ZipCrypto archive whose
password is b"test". Timing and allocation measurements use separate runs.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import statistics
import time
import tracemalloc
import typing
import zipfile
from pathlib import Path
from random import Random

from zipwire import AsyncRemoteZip, SyncRemoteWheel

if typing.TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator


class MemoryReader:
    url = "https://example.com/demo-1.0-py3-none-any.whl"

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.read_count = 0

    def head(self) -> dict[str, str]:
        return {"content-length": str(len(self.data))}

    def read_range(self, offset: int, length: int) -> tuple[bytes, dict[str, str]]:
        self.read_count += 1
        return self.data[offset : offset + length], {}

    def stream_range(self, offset: int, length: int) -> Iterator[bytes]:
        self.read_count += 1
        for start in range(offset, offset + length, 2 * 1024 * 1024):
            yield self.data[start : min(start + 2 * 1024 * 1024, offset + length)]

    def close(self) -> None:
        pass


class AsyncMemoryReader:
    def __init__(self, data: bytes) -> None:
        self.reader = MemoryReader(data)
        self.url = self.reader.url

    async def head(self) -> dict[str, str]:
        return self.reader.head()

    async def read_range(self, offset: int, length: int) -> tuple[bytes, dict[str, str]]:
        return self.reader.read_range(offset, length)

    async def stream_range(self, offset: int, length: int) -> AsyncIterator[bytes]:
        for chunk in self.reader.stream_range(offset, length):
            yield chunk

    async def close(self) -> None:
        pass


class Sink:
    def __init__(self) -> None:
        self.size = 0

    def write(self, data: bytes) -> None:
        self.size += len(data)


class FullTailWheel(SyncRemoteWheel):
    def _tail_read_size(self, file_size: int) -> int:
        # Exercise a large cached tail without constructing a multi-GB wheel.
        return file_size


def archive(files: dict[str, bytes], method: int = zipfile.ZIP_STORED) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=method) as writer:
        for name, data in files.items():
            writer.writestr(name, data)
    return buffer.getvalue()


async def extract(reader: AsyncRemoteZip, *, into: bool) -> None:
    info = reader.infolist()[0]
    if into:
        sink = Sink()
        await reader.read_into(info, sink, pwd=b"test")
        assert sink.size == info.file_size
    else:
        assert len(await reader.read(info, pwd=b"test")) == info.file_size


async def heartbeat(stopped: asyncio.Event, gaps: list[float]) -> None:
    previous = time.perf_counter()
    while not stopped.is_set():
        await asyncio.sleep(0)
        current = time.perf_counter()
        gaps.append(current - previous)
        previous = current


async def measure(data: bytes, *, into: bool, repeats: int) -> dict[str, float]:
    times = []
    gaps = []
    async with AsyncRemoteZip(AsyncMemoryReader(data)) as reader:
        size = reader.infolist()[0].file_size
        for _ in range(repeats):
            stopped = asyncio.Event()
            task = asyncio.create_task(heartbeat(stopped, gaps))
            await asyncio.sleep(0)
            try:
                start = time.perf_counter()
                await extract(reader, into=into)
                times.append(time.perf_counter() - start)
            finally:
                stopped.set()
                await task
        tracemalloc.start()
        try:
            await extract(reader, into=into)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
    elapsed = statistics.median(times)
    return {
        "median_ms": round(elapsed * 1000, 3),
        "mib_per_second": round(size / 1024**2 / elapsed, 2),
        "max_loop_gap_ms": round(max(gaps) * 1000, 3),
        "peak_mib": round(peak / 1024**2, 3),
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--encrypted-zip", type=Path)
    args = parser.parse_args()
    cases = {
        "stored_16m": archive({"file": Random(0).randbytes(16 * 1024**2)}),
        "deflate_64m": archive({"file": b"x" * (64 * 1024**2)}, zipfile.ZIP_DEFLATED),
    }
    if args.encrypted_zip:
        cases["encrypted"] = args.encrypted_zip.read_bytes()
    for name, data in cases.items():
        for into in (False, True):
            result = await measure(data, into=into, repeats=args.repeats)
            print(json.dumps({"case": name, "method": "read_into" if into else "read", **result}))

    reader = MemoryReader(archive({"file": b"payload"}))
    with SyncRemoteWheel(reader) as wheel:
        loading = reader.read_count
        wheel.read("file")
        print(
            json.dumps(
                {
                    "case": "small_wheel",
                    "load_ranges": loading,
                    "member_ranges": reader.read_count - loading,
                }
            )
        )

    data = archive({"prefix": b"", "file": b"payload", "padding": b"x" * (8 * 1024**2)})
    with FullTailWheel(MemoryReader(data)) as wheel:
        tracemalloc.start()
        try:
            assert wheel.read("file") == b"payload"
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        print(json.dumps({"case": "cached_header_8m", "peak_mib": round(peak / 1024**2, 3)}))


if __name__ == "__main__":
    asyncio.run(main())
