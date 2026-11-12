"""Tests for AsyncRemoteZip with MockAsyncReader."""

from __future__ import annotations

import io
import struct
import typing
import zipfile

import pytest

from tests.conftest import MockAsyncReader, make_zip
from zipwire import AsyncRemoteZip, EOCDInfo
from zipwire._constants import (
    FLAG_COMPRESSED_PATCHED_DATA,
    FLAG_ENCRYPTED,
    LOCAL_FILE_HEADER_SIZE,
    STREAM_CHUNK_SIZE,
)
from zipwire._errors import (
    BadZipFile,
    FileNotFoundInZip,
    FileTooLarge,
    RangeRequestUnsupported,
    UnsupportedFeature,
)
from zipwire._parser import find_eocd
from zipwire._zipinfo import RemoteZipInfo

if typing.TYPE_CHECKING:
    from collections.abc import AsyncGenerator


class TestAsyncRemoteZip:
    async def test_namelist(self, stored_zip: bytes) -> None:
        reader = MockAsyncReader(stored_zip)
        async with AsyncRemoteZip(reader) as rz:
            names = rz.namelist()
        assert "hello.txt" in names
        assert "data/numbers.bin" in names

    async def test_infolist(self, stored_zip: bytes) -> None:
        reader = MockAsyncReader(stored_zip)
        async with AsyncRemoteZip(reader) as rz:
            infos = rz.infolist()
        assert len(infos) == 2
        for info in infos:
            assert isinstance(info, RemoteZipInfo)
            assert isinstance(info, zipfile.ZipInfo)

    async def test_getinfo(self, stored_zip: bytes) -> None:
        reader = MockAsyncReader(stored_zip)
        async with AsyncRemoteZip(reader) as rz:
            info = rz.getinfo("hello.txt")
        assert info.filename == "hello.txt"
        assert info.file_size == 13

    async def test_getinfo_not_found(self, stored_zip: bytes) -> None:
        reader = MockAsyncReader(stored_zip)
        async with AsyncRemoteZip(reader) as rz:
            with pytest.raises(FileNotFoundInZip):
                rz.getinfo("nonexistent.txt")

    async def test_read_stored(self, stored_zip: bytes) -> None:
        reader = MockAsyncReader(stored_zip)
        async with AsyncRemoteZip(reader) as rz:
            data = await rz.read("hello.txt")
        assert data == b"Hello, World!"

    async def test_read_stored_binary(self, stored_zip: bytes) -> None:
        reader = MockAsyncReader(stored_zip)
        async with AsyncRemoteZip(reader) as rz:
            data = await rz.read("data/numbers.bin")
        assert data == b"\x01\x02\x03\x04\x05"

    async def test_read_deflated(self, deflated_zip: bytes) -> None:
        reader = MockAsyncReader(deflated_zip)
        async with AsyncRemoteZip(reader) as rz:
            data = await rz.read("hello.txt")
        assert data == b"Hello, World!"

    async def test_read_deflated_repeated(self, deflated_zip: bytes) -> None:
        reader = MockAsyncReader(deflated_zip)
        async with AsyncRemoteZip(reader) as rz:
            data = await rz.read("repeated.txt")
        assert data == b"AAAA" * 1000

    async def test_read_by_info(self, stored_zip: bytes) -> None:
        reader = MockAsyncReader(stored_zip)
        async with AsyncRemoteZip(reader) as rz:
            info = rz.getinfo("hello.txt")
            data = await rz.read(info)
        assert data == b"Hello, World!"

    async def test_read_directory(self, directory_zip: bytes) -> None:
        reader = MockAsyncReader(directory_zip)
        async with AsyncRemoteZip(reader) as rz:
            names = rz.namelist()
            for name in names:
                info = rz.getinfo(name)
                if info.is_dir():
                    assert await rz.read(info) == b""

    async def test_close(self, stored_zip: bytes) -> None:
        reader = MockAsyncReader(stored_zip)
        rz = AsyncRemoteZip(reader)
        await rz.close()
        assert reader.closed

    async def test_context_manager_closes(self, stored_zip: bytes) -> None:
        reader = MockAsyncReader(stored_zip)
        async with AsyncRemoteZip(reader) as rz:
            rz.namelist()
        assert reader.closed

    async def test_aenter_closes_reader_on_load_failure(self) -> None:
        reader = MockAsyncReader(b"not a zip file")
        with pytest.raises(BadZipFile):
            async with AsyncRemoteZip(reader):
                pass
        assert reader.closed

    async def test_aenter_close_error_chains(self) -> None:
        """A failing reader close() chains the load error as context."""

        class FailingCloseReader(MockAsyncReader):
            async def close(self) -> None:
                raise OSError("close failed")

        reader = FailingCloseReader(b"not a zip file")
        with pytest.raises(OSError, match="close failed") as excinfo:
            async with AsyncRemoteZip(reader):
                pass
        assert isinstance(excinfo.value.__context__, BadZipFile)

    async def test_file_size(self, stored_zip: bytes) -> None:
        reader = MockAsyncReader(stored_zip)
        async with AsyncRemoteZip(reader) as rz:
            assert rz.file_size == len(stored_zip)

    async def test_eocd_info(self, stored_zip: bytes) -> None:
        reader = MockAsyncReader(stored_zip)
        async with AsyncRemoteZip(reader) as rz:
            eocd = rz.eocd_info
        assert isinstance(eocd, EOCDInfo)
        assert eocd.cd_entry_count == 2
        assert eocd.cd_size > 0
        assert 0 <= eocd.cd_offset < len(stored_zip)

    async def test_url(self, stored_zip: bytes) -> None:
        reader = MockAsyncReader(stored_zip, url="https://example.com/archive.zip")
        async with AsyncRemoteZip(reader) as rz:
            assert rz.url == "https://example.com/archive.zip"
            assert reader.url == "https://example.com/archive.zip"

    async def test_loading_on_enter(self, stored_zip: bytes) -> None:
        reader = MockAsyncReader(stored_zip)
        rz = AsyncRemoteZip(reader)
        assert reader.read_count == 0
        async with rz:
            assert reader.read_count > 0
            # _ensure_loaded cannot be called twice
            with pytest.raises(RuntimeError, match="must not be called more than once"):
                await rz._ensure_loaded()

    async def test_not_loaded_raises(self, stored_zip: bytes) -> None:
        reader = MockAsyncReader(stored_zip)
        rz = AsyncRemoteZip(reader)
        for call in [
            lambda: rz.file_size,
            lambda: rz.eocd_info,
            lambda: rz.namelist(),
            lambda: rz.infolist(),
            lambda: rz.getinfo("hello.txt"),
        ]:
            with pytest.raises(RuntimeError, match="not loaded"):
                call()
        for coro_fn in [
            lambda: rz.read("hello.txt"),
            lambda: rz.read_into("hello.txt", io.BytesIO()),
        ]:
            with pytest.raises(RuntimeError, match="not loaded"):
                await coro_fn()
        await rz.close()

    async def test_unicode_filenames(self, unicode_zip: bytes) -> None:
        reader = MockAsyncReader(unicode_zip)
        async with AsyncRemoteZip(reader) as rz:
            names = rz.namelist()
        assert "\u00e9\u00e0\u00fc.txt" in names
        assert "\u4f60\u597d/world.txt" in names

    async def test_empty_zip(self, empty_zip: bytes) -> None:
        reader = MockAsyncReader(empty_zip)
        async with AsyncRemoteZip(reader) as rz:
            assert rz.namelist() == []
            assert rz.infolist() == []

    async def test_comment_zip(self, comment_zip: bytes) -> None:
        reader = MockAsyncReader(comment_zip)
        async with AsyncRemoteZip(reader) as rz:
            assert rz.namelist() == ["file.txt"]
            assert await rz.read("file.txt") == b"content"

    async def test_mixed_compression(self, mixed_zip: bytes) -> None:
        reader = MockAsyncReader(mixed_zip)
        async with AsyncRemoteZip(reader) as rz:
            assert await rz.read("stored.txt") == b"stored content"
            assert await rz.read("deflated.txt") == b"deflated " * 100

    async def test_read_into_stored(self, stored_zip: bytes) -> None:
        reader = MockAsyncReader(stored_zip)
        async with AsyncRemoteZip(reader) as rz:
            dest = io.BytesIO()
            await rz.read_into("hello.txt", dest)
        assert dest.getvalue() == b"Hello, World!"

    async def test_read_into_stored_binary(self, stored_zip: bytes) -> None:
        reader = MockAsyncReader(stored_zip)
        async with AsyncRemoteZip(reader) as rz:
            dest = io.BytesIO()
            await rz.read_into("data/numbers.bin", dest)
        assert dest.getvalue() == b"\x01\x02\x03\x04\x05"

    async def test_read_into_deflated(self, deflated_zip: bytes) -> None:
        reader = MockAsyncReader(deflated_zip)
        async with AsyncRemoteZip(reader) as rz:
            dest = io.BytesIO()
            await rz.read_into("hello.txt", dest)
        assert dest.getvalue() == b"Hello, World!"

    async def test_read_into_deflated_repeated(self, deflated_zip: bytes) -> None:
        reader = MockAsyncReader(deflated_zip)
        async with AsyncRemoteZip(reader) as rz:
            dest = io.BytesIO()
            await rz.read_into("repeated.txt", dest)
        assert dest.getvalue() == b"AAAA" * 1000

    async def test_read_into_by_info(self, stored_zip: bytes) -> None:
        reader = MockAsyncReader(stored_zip)
        async with AsyncRemoteZip(reader) as rz:
            info = rz.getinfo("hello.txt")
            dest = io.BytesIO()
            await rz.read_into(info, dest)
        assert dest.getvalue() == b"Hello, World!"

    async def test_read_into_directory(self, directory_zip: bytes) -> None:
        reader = MockAsyncReader(directory_zip)
        async with AsyncRemoteZip(reader) as rz:
            names = rz.namelist()
            for name in names:
                info = rz.getinfo(name)
                if info.is_dir():
                    dest = io.BytesIO()
                    await rz.read_into(info, dest)
                    assert dest.getvalue() == b""

    async def test_read_into_mixed_compression(self, mixed_zip: bytes) -> None:
        reader = MockAsyncReader(mixed_zip)
        async with AsyncRemoteZip(reader) as rz:
            stored_dest = io.BytesIO()
            await rz.read_into("stored.txt", stored_dest)
            assert stored_dest.getvalue() == b"stored content"

            deflated_dest = io.BytesIO()
            await rz.read_into("deflated.txt", deflated_dest)
            assert deflated_dest.getvalue() == b"deflated " * 100

    async def test_matches_stdlib(self, stored_zip: bytes) -> None:
        """Verify we read the same data as stdlib zipfile."""
        reader = MockAsyncReader(stored_zip)
        async with AsyncRemoteZip(reader) as rz:
            names = rz.namelist()
            our_data = {}
            for name in names:
                our_data[name] = await rz.read(name)

        with zipfile.ZipFile(io.BytesIO(stored_zip)) as zf:
            stdlib_data = {name: zf.read(name) for name in zf.namelist()}

        assert our_data == stdlib_data

    async def test_read_large_file(self, large_file_zip: bytes) -> None:
        """File > 50 KiB bypasses prefetch, uses separate read_range for data."""
        expected = bytes(range(256)) * 208
        reader = MockAsyncReader(large_file_zip)
        async with AsyncRemoteZip(reader) as rz:
            data = await rz.read("large.bin")
        assert data == expected

    async def test_read_into_large_file(self, large_file_zip: bytes) -> None:
        """File > 50 KiB uses stream_range for streaming decompression."""
        expected = bytes(range(256)) * 208
        reader = MockAsyncReader(large_file_zip)
        async with AsyncRemoteZip(reader) as rz:
            dest = io.BytesIO()
            await rz.read_into("large.bin", dest)
        assert dest.getvalue() == expected

    async def test_read_into_bounded_output_chunks(self) -> None:
        """Decoded output reaches the destination in bounded chunks."""
        content = b"\0" * (2 * STREAM_CHUNK_SIZE + 123)
        sizes: list[int] = []
        output = bytearray()

        class Destination:
            def write(self, chunk: bytes) -> int:
                sizes.append(len(chunk))
                output.extend(chunk)
                return len(chunk)

        archive_data = make_zip({"entry.bin": content}, compression=zipfile.ZIP_DEFLATED)
        async with AsyncRemoteZip(MockAsyncReader(archive_data)) as rz:
            await rz.read_into("entry.bin", Destination())
        assert bytes(output) == content
        assert max(sizes) <= STREAM_CHUNK_SIZE

    def test_read_into_is_loop_agnostic(self) -> None:
        """read_into must not use event-loop primitives such as asyncio.sleep(0).

        A checkpoint like asyncio.sleep(0) passes under asyncio but raises
        RuntimeError on other event loops such as trio.  Driving the
        coroutine by hand, with no event loop at all, detects any await
        that needs one: the extraction must run to completion without ever
        suspending, for both sync and async destinations.
        """
        content = b"\0" * (3 * STREAM_CHUNK_SIZE)

        class SyncDestination:
            def __init__(self) -> None:
                self.data = bytearray()

            def write(self, chunk: bytes) -> int:
                self.data.extend(chunk)
                return len(chunk)

        class AsyncDestination:
            def __init__(self) -> None:
                self.data = bytearray()

            async def write(self, chunk: bytes) -> int:
                self.data.extend(chunk)
                return len(chunk)

        archive_data = make_zip({"entry.bin": content}, compression=zipfile.ZIP_DEFLATED)
        for destination in (SyncDestination(), AsyncDestination()):
            coroutine = self._extract(archive_data, destination)
            try:
                coroutine.send(None)
            except StopIteration:
                pass
            else:
                coroutine.close()
                pytest.fail("read_into suspended on an event-loop primitive")
            assert bytes(destination.data) == content

    @staticmethod
    async def _extract(archive_data: bytes, destination: typing.Any) -> None:
        async with AsyncRemoteZip(MockAsyncReader(archive_data)) as rz:
            await rz.read_into("entry.bin", destination)

    async def test_read_into_closes_stream_on_error(self, large_file_zip: bytes) -> None:
        """Close the response generator when writing fails mid-stream."""

        class ClosingReader(MockAsyncReader):
            stream_closed = False

            async def stream_range(self, offset: int, length: int) -> AsyncGenerator[bytes, None]:
                try:
                    async for chunk in super().stream_range(offset, length):
                        yield chunk
                finally:
                    self.stream_closed = True

        class FailingDest:
            def write(self, data: bytes) -> None:
                raise OSError("write failed")

        reader = ClosingReader(large_file_zip)
        async with AsyncRemoteZip(reader) as rz:
            with pytest.raises(OSError, match="write failed"):
                await rz.read_into("large.bin", FailingDest())
        assert reader.stream_closed

    async def test_read_into_close_error_chains(self, large_file_zip: bytes) -> None:
        """A failing stream aclose() masks the extraction error.

        Unlike the sync path, the original error is not reachable in
        the exception chain here: the async generator machinery drops
        it when aclose() raises.
        """

        class FailingCloseReader(MockAsyncReader):
            async def stream_range(self, offset: int, length: int) -> AsyncGenerator[bytes, None]:
                try:
                    async for chunk in super().stream_range(offset, length):
                        yield chunk
                finally:
                    raise OSError("close failed")

        class FailingDest:
            def write(self, data: bytes) -> None:
                raise OSError("write failed")

        reader = FailingCloseReader(large_file_zip)
        async with AsyncRemoteZip(reader) as rz:
            with pytest.raises(OSError, match="close failed"):
                await rz.read_into("large.bin", FailingDest())

    async def test_read_zstandard(self, zstandard_zip: bytes) -> None:
        reader = MockAsyncReader(zstandard_zip)
        async with AsyncRemoteZip(reader) as rz:
            assert await rz.read("hello.txt") == b"Hello, World!"
            assert await rz.read("repeated.txt") == b"AAAA" * 1000

    async def test_read_into_zstandard(self, zstandard_zip: bytes) -> None:
        reader = MockAsyncReader(zstandard_zip)
        async with AsyncRemoteZip(reader) as rz:
            dest = io.BytesIO()
            await rz.read_into("hello.txt", dest)
            assert dest.getvalue() == b"Hello, World!"

            dest = io.BytesIO()
            await rz.read_into("repeated.txt", dest)
            assert dest.getvalue() == b"AAAA" * 1000

    @pytest.mark.parametrize("method", ["read", "read_into"])
    async def test_file_too_large(self, stored_zip: bytes, method: str) -> None:
        reader = MockAsyncReader(stored_zip)
        async with AsyncRemoteZip(reader) as rz:
            with pytest.raises(FileTooLarge):
                if method == "read":
                    await rz.read("hello.txt", max_file_size=1)
                else:
                    await rz.read_into("hello.txt", io.BytesIO(), max_file_size=1)

    async def test_no_content_length(self, stored_zip: bytes) -> None:
        reader = MockAsyncReader(stored_zip)

        async def _head_no_cl() -> dict[str, str]:
            return {"accept-ranges": "bytes"}

        reader.head = _head_no_cl
        with pytest.raises(RangeRequestUnsupported, match="Content-Length"):
            async with AsyncRemoteZip(reader):
                pass


class ShortTailAsyncReader(MockAsyncReader):
    """Reader whose first (tail) read_range returns one byte too few."""

    async def read_range(
        self,
        offset: int,
        length: int,
    ) -> tuple[bytes, dict[str, str]]:
        data, headers = await super().read_range(offset, length)
        if self.read_count == 1:
            data = data[:-1]
        return data, headers


class ShortFilenameAsyncReader(MockAsyncReader):
    """Reader whose fourth (local header filename) read_range is short."""

    async def read_range(
        self,
        offset: int,
        length: int,
    ) -> tuple[bytes, dict[str, str]]:
        data, headers = await super().read_range(offset, length)
        if self.read_count == 4:
            data = data[:-1]
        return data, headers


class TestHardening:
    """Admission and bounds checks ported from zipfile (async path)."""

    @pytest.mark.parametrize("method", ["read", "read_into"])
    async def test_local_name_mismatch_rejected(self, stored_zip: bytes, method: str) -> None:
        # The local header names a different member than the central
        # directory ("zip confusion"); rejected like zipfile does.
        buf = bytearray(stored_zip)
        eocd = find_eocd(stored_zip, len(stored_zip))
        header_offset = struct.unpack_from("<I", buf, eocd.cd_offset + 42)[0]
        # Local file header: filename_len is at offset 26.
        fn_len = struct.unpack_from("<H", buf, header_offset + 26)[0]
        start = header_offset + LOCAL_FILE_HEADER_SIZE
        buf[start : start + fn_len] = b"hello.txX"
        reader = MockAsyncReader(bytes(buf))
        async with AsyncRemoteZip(reader) as rz:
            with pytest.raises(BadZipFile, match="differ"):
                if method == "read":
                    await rz.read("hello.txt")
                else:
                    await rz.read_into("hello.txt", io.BytesIO())

    async def test_long_local_name_fetched_for_check(self) -> None:
        # A local filename longer than the PREFETCH_EXTRA allowance is
        # fetched with an extra range request and must still match.
        name = "l" * 1200 + ".txt"
        data = make_zip({name: b"long name content"})
        reader = MockAsyncReader(data)
        async with AsyncRemoteZip(reader) as rz:
            assert await rz.read(name) == b"long name content"
        assert (LOCAL_FILE_HEADER_SIZE, len(name)) in reader.requests

    async def test_long_local_name_mismatch_rejected(self) -> None:
        # A mismatching local filename must not slip past the check just
        # because it is longer than the prefetch allowance.
        name = "l" * 1200 + ".txt"
        data = make_zip({name: b"long name content"})
        buf = bytearray(data)
        header_offset = 0
        fn_len = struct.unpack_from("<H", buf, header_offset + 26)[0]
        start = header_offset + LOCAL_FILE_HEADER_SIZE
        buf[start : start + fn_len] = ("L" + name[1:]).encode()
        reader = MockAsyncReader(bytes(buf))
        async with AsyncRemoteZip(reader) as rz:
            with pytest.raises(BadZipFile, match="differ"):
                await rz.read(name)

    async def test_encrypted_entry_rejected(self, stored_zip: bytes) -> None:
        buf = bytearray(stored_zip)
        eocd = find_eocd(stored_zip, len(stored_zip))
        offset = eocd.cd_offset
        flags = struct.unpack_from("<H", buf, offset + 8)[0]
        struct.pack_into("<H", buf, offset + 8, flags | FLAG_ENCRYPTED)
        reader = MockAsyncReader(bytes(buf))
        async with AsyncRemoteZip(reader) as rz:
            with pytest.raises(UnsupportedFeature, match="encrypted"):
                await rz.read("hello.txt")

    async def test_patched_data_rejected(self, stored_zip: bytes) -> None:
        buf = bytearray(stored_zip)
        eocd = find_eocd(stored_zip, len(stored_zip))
        offset = eocd.cd_offset
        flags = struct.unpack_from("<H", buf, offset + 8)[0]
        struct.pack_into("<H", buf, offset + 8, flags | FLAG_COMPRESSED_PATCHED_DATA)
        reader = MockAsyncReader(bytes(buf))
        async with AsyncRemoteZip(reader) as rz:
            with pytest.raises(UnsupportedFeature, match="patched data"):
                await rz.read("hello.txt")

    async def test_overlapped_entry_rejected(self, stored_zip: bytes) -> None:
        # Inflate compress_size so the data runs past the next entry's
        # local header, but stays inside the archive.
        buf = bytearray(stored_zip)
        eocd = find_eocd(stored_zip, len(stored_zip))
        struct.pack_into("<I", buf, eocd.cd_offset + 20, 100)
        reader = MockAsyncReader(bytes(buf))
        async with AsyncRemoteZip(reader) as rz:
            with pytest.raises(BadZipFile, match="Overlapped entries"):
                await rz.read("hello.txt")

    async def test_cd_bounds_checked_before_fetch(self, stored_zip: bytes) -> None:
        # A hostile cd_size must not become a range request: only the
        # initial tail fetch is allowed before the archive is rejected.
        buf = bytearray(stored_zip)
        eocd_pos = buf.rfind(b"PK\x05\x06")
        struct.pack_into("<I", buf, eocd_pos + 12, 0xFFFFFF00)
        data = bytes(buf)
        reader = MockAsyncReader(data)
        with pytest.raises(BadZipFile, match="beyond the end of the archive"):
            async with AsyncRemoteZip(reader):
                pass
        assert reader.requests == [(0, len(data))]

    async def test_prefetch_fetch_stays_in_bounds(self, stored_zip: bytes) -> None:
        # A lying compress_size below the prefetch threshold must not
        # become a range request past the end of the archive either.
        buf = bytearray(stored_zip)
        eocd = find_eocd(stored_zip, len(stored_zip))
        struct.pack_into("<I", buf, eocd.cd_offset + 20, 5000)
        data = bytes(buf)
        reader = MockAsyncReader(data)
        async with AsyncRemoteZip(reader) as rz:
            with pytest.raises(BadZipFile, match="beyond the end of the archive"):
                await rz.read("hello.txt")
        assert all(offset + length <= len(data) for offset, length in reader.requests)

    async def test_cd_beyond_archive_rejected_on_load(self, stored_zip: bytes) -> None:
        buf = bytearray(stored_zip)
        eocd_pos = buf.rfind(b"PK\x05\x06")
        cd_offset = struct.unpack_from("<I", buf, eocd_pos + 16)[0]
        struct.pack_into("<I", buf, eocd_pos + 16, cd_offset + 1000)
        reader = MockAsyncReader(bytes(buf))
        with pytest.raises(BadZipFile, match="beyond the end of the archive"):
            async with AsyncRemoteZip(reader):
                pass

    async def test_short_tail_read_rejected(self, stored_zip: bytes) -> None:
        reader = ShortTailAsyncReader(stored_zip)
        with pytest.raises(BadZipFile, match="tail fetch returned"):
            async with AsyncRemoteZip(reader):
                pass

    async def test_short_local_filename_fetch_rejected(self) -> None:
        # The extra fetch for a filename longer than the prefetch
        # allowance must return every byte it asked for.
        name = "l" * 1200 + ".txt"
        data = make_zip({name: b"long name content"})
        reader = ShortFilenameAsyncReader(data)
        async with AsyncRemoteZip(reader) as rz:
            with pytest.raises(BadZipFile, match="filename fetch returned"):
                await rz.read(name)
