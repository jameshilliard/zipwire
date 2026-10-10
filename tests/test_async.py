"""Tests for AsyncRemoteZip with MockAsyncReader."""

from __future__ import annotations

import asyncio
import io
import typing
import zipfile
import zlib

import pytest

from tests.conftest import MockAsyncReader
from zipwire import AsyncRemoteZip, EOCDInfo
from zipwire._errors import FileNotFoundInZip, FileTooLarge, RangeRequestUnsupported
from zipwire._zipinfo import RemoteZipInfo

if typing.TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator


class FailingWriter:
    def write(self, data: bytes) -> None:
        raise OSError("destination write failed")


class IteratorWithoutClose:
    """A resource-free async iterator, allowed by the reader protocol."""

    def __init__(self, data: bytes) -> None:
        self.data: bytes | None = data

    def __aiter__(self) -> IteratorWithoutClose:
        return self

    async def __anext__(self) -> bytes:
        if self.data is None:
            raise StopAsyncIteration
        data, self.data = self.data, None
        return data


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

    @pytest.mark.parametrize("failure", ["write", "decompress", "size_limit"])
    async def test_read_into_failure_closes_stream(
        self, large_file_zip: bytes, monkeypatch: pytest.MonkeyPatch, failure: str
    ) -> None:
        reader = MockAsyncReader(large_file_zip)
        closed = False

        async def chunks(offset: int, length: int) -> AsyncGenerator[bytes, None]:
            nonlocal closed
            try:
                yield reader.data[offset : offset + length]
            finally:
                await asyncio.sleep(0)
                closed = True

        async with AsyncRemoteZip(reader) as rz:
            info = rz.getinfo("large.bin")
            offset = info.header_offset + len(info.FileHeader())
            # Retain the generator so collection cannot hide missing cleanup.
            stream = chunks(offset, info.compress_size)
            monkeypatch.setattr(reader, "stream_range", lambda offset, length: stream)
            error: type[Exception]
            if failure == "decompress":
                info.compress_type = zipfile.ZIP_DEFLATED
                error = zlib.error
            elif failure == "size_limit":
                info.file_size = 0  # Force the limit to be checked during streaming.
                error = FileTooLarge
            else:
                error = OSError
            try:
                with pytest.raises(error):
                    await rz.read_into(
                        info,
                        FailingWriter() if failure == "write" else io.BytesIO(),
                        max_file_size=1 if failure == "size_limit" else None,
                    )
                assert closed
                assert not reader.closed
            finally:
                await stream.aclose()

    @pytest.mark.parametrize("fail_write", [False, True])
    async def test_read_into_iterator_without_close(
        self, large_file_zip: bytes, monkeypatch: pytest.MonkeyPatch, fail_write: bool
    ) -> None:
        reader = MockAsyncReader(large_file_zip)

        def stream_range(offset: int, length: int) -> AsyncIterator[bytes]:
            return IteratorWithoutClose(reader.data[offset : offset + length])

        monkeypatch.setattr(reader, "stream_range", stream_range)
        async with AsyncRemoteZip(reader) as rz:
            if fail_write:
                with pytest.raises(OSError, match="destination write failed"):
                    await rz.read_into("large.bin", FailingWriter())
            else:
                dest = io.BytesIO()
                await rz.read_into("large.bin", dest)
                assert dest.getvalue() == bytes(range(256)) * 208
            assert not reader.closed

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
