"""Tests for SyncRemoteZip with MockSyncReader."""

from __future__ import annotations

import io
import struct
import typing
import zipfile

import pytest

from tests.conftest import MockSyncReader, make_zip
from zipwire import EOCDInfo, SyncRemoteZip
from zipwire._constants import (
    CENTRAL_DIR_SIZE,
    FLAG_COMPRESSED_PATCHED_DATA,
    FLAG_ENCRYPTED,
    FLAG_STRONG_ENCRYPTION,
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
    from collections.abc import Generator


class TestSyncRemoteZip:
    def test_namelist(self, stored_zip: bytes) -> None:
        reader = MockSyncReader(stored_zip)
        with SyncRemoteZip(reader) as rz:
            names = rz.namelist()
        assert "hello.txt" in names
        assert "data/numbers.bin" in names

    def test_infolist(self, stored_zip: bytes) -> None:
        reader = MockSyncReader(stored_zip)
        with SyncRemoteZip(reader) as rz:
            infos = rz.infolist()
        assert len(infos) == 2
        for info in infos:
            assert isinstance(info, RemoteZipInfo)
            assert isinstance(info, zipfile.ZipInfo)

    def test_getinfo(self, stored_zip: bytes) -> None:
        reader = MockSyncReader(stored_zip)
        with SyncRemoteZip(reader) as rz:
            info = rz.getinfo("hello.txt")
        assert info.filename == "hello.txt"
        assert info.file_size == 13

    def test_file_size(self, stored_zip: bytes) -> None:
        reader = MockSyncReader(stored_zip)
        with SyncRemoteZip(reader) as rz:
            assert rz.file_size == len(stored_zip)

    def test_eocd_info(self, stored_zip: bytes) -> None:
        reader = MockSyncReader(stored_zip)
        with SyncRemoteZip(reader) as rz:
            eocd = rz.eocd_info
        assert isinstance(eocd, EOCDInfo)
        assert eocd.cd_entry_count == 2
        assert eocd.cd_size > 0
        assert 0 <= eocd.cd_offset < len(stored_zip)

    def test_url(self, stored_zip: bytes) -> None:
        reader = MockSyncReader(stored_zip, url="https://example.com/archive.zip")
        with SyncRemoteZip(reader) as rz:
            assert rz.url == "https://example.com/archive.zip"
            assert reader.url == "https://example.com/archive.zip"

    def test_getinfo_not_found(self, stored_zip: bytes) -> None:
        reader = MockSyncReader(stored_zip)
        with SyncRemoteZip(reader) as rz, pytest.raises(FileNotFoundInZip):
            rz.getinfo("nonexistent.txt")

    def test_read_stored(self, stored_zip: bytes) -> None:
        reader = MockSyncReader(stored_zip)
        with SyncRemoteZip(reader) as rz:
            data = rz.read("hello.txt")
        assert data == b"Hello, World!"

    def test_read_stored_binary(self, stored_zip: bytes) -> None:
        reader = MockSyncReader(stored_zip)
        with SyncRemoteZip(reader) as rz:
            data = rz.read("data/numbers.bin")
        assert data == b"\x01\x02\x03\x04\x05"

    def test_read_deflated(self, deflated_zip: bytes) -> None:
        reader = MockSyncReader(deflated_zip)
        with SyncRemoteZip(reader) as rz:
            data = rz.read("hello.txt")
        assert data == b"Hello, World!"

    def test_read_deflated_repeated(self, deflated_zip: bytes) -> None:
        reader = MockSyncReader(deflated_zip)
        with SyncRemoteZip(reader) as rz:
            data = rz.read("repeated.txt")
        assert data == b"AAAA" * 1000

    def test_read_by_info(self, stored_zip: bytes) -> None:
        reader = MockSyncReader(stored_zip)
        with SyncRemoteZip(reader) as rz:
            info = rz.getinfo("hello.txt")
            data = rz.read(info)
        assert data == b"Hello, World!"

    def test_read_directory(self, directory_zip: bytes) -> None:
        reader = MockSyncReader(directory_zip)
        with SyncRemoteZip(reader) as rz:
            names = rz.namelist()
            for name in names:
                info = rz.getinfo(name)
                if info.is_dir():
                    assert rz.read(info) == b""

    def test_close(self, stored_zip: bytes) -> None:
        reader = MockSyncReader(stored_zip)
        rz = SyncRemoteZip(reader)
        rz.close()
        assert reader.closed

    def test_context_manager_closes(self, stored_zip: bytes) -> None:
        reader = MockSyncReader(stored_zip)
        with SyncRemoteZip(reader) as rz:
            rz.namelist()
        assert reader.closed

    def test_enter_closes_reader_on_load_failure(self) -> None:
        reader = MockSyncReader(b"not a zip file")
        with pytest.raises(BadZipFile), SyncRemoteZip(reader):
            pass
        assert reader.closed

    def test_enter_close_error_chains(self) -> None:
        """A failing reader close() chains the load error as context."""

        class FailingCloseReader(MockSyncReader):
            def close(self) -> None:
                raise OSError("close failed")

        reader = FailingCloseReader(b"not a zip file")
        with pytest.raises(OSError, match="close failed") as excinfo, SyncRemoteZip(reader):
            pass
        assert isinstance(excinfo.value.__context__, BadZipFile)

    def test_loading_on_enter(self, stored_zip: bytes) -> None:
        reader = MockSyncReader(stored_zip)
        rz = SyncRemoteZip(reader)
        # No requests made yet
        assert reader.read_count == 0
        # __enter__ triggers loading
        with rz:
            assert reader.read_count > 0
            # _ensure_loaded cannot be called twice
            with pytest.raises(RuntimeError, match="must not be called more than once"):
                rz._ensure_loaded()

    def test_not_loaded_raises(self, stored_zip: bytes) -> None:
        reader = MockSyncReader(stored_zip)
        rz = SyncRemoteZip(reader)
        for call in [
            lambda: rz.file_size,
            lambda: rz.eocd_info,
            lambda: rz.namelist(),
            lambda: rz.infolist(),
            lambda: rz.getinfo("hello.txt"),
            lambda: rz.read("hello.txt"),
            lambda: rz.read_into("hello.txt", io.BytesIO()),
        ]:
            with pytest.raises(RuntimeError, match="not loaded"):
                call()
        rz.close()

    def test_unicode_filenames(self, unicode_zip: bytes) -> None:
        reader = MockSyncReader(unicode_zip)
        with SyncRemoteZip(reader) as rz:
            names = rz.namelist()
        assert "\u00e9\u00e0\u00fc.txt" in names
        assert "\u4f60\u597d/world.txt" in names

    def test_empty_zip(self, empty_zip: bytes) -> None:
        reader = MockSyncReader(empty_zip)
        with SyncRemoteZip(reader) as rz:
            assert rz.namelist() == []
            assert rz.infolist() == []

    def test_comment_zip(self, comment_zip: bytes) -> None:
        reader = MockSyncReader(comment_zip)
        with SyncRemoteZip(reader) as rz:
            assert rz.namelist() == ["file.txt"]
            assert rz.read("file.txt") == b"content"

    def test_mixed_compression(self, mixed_zip: bytes) -> None:
        reader = MockSyncReader(mixed_zip)
        with SyncRemoteZip(reader) as rz:
            assert rz.read("stored.txt") == b"stored content"
            assert rz.read("deflated.txt") == b"deflated " * 100

    def test_read_into_stored(self, stored_zip: bytes) -> None:
        reader = MockSyncReader(stored_zip)
        with SyncRemoteZip(reader) as rz:
            dest = io.BytesIO()
            rz.read_into("hello.txt", dest)
        assert dest.getvalue() == b"Hello, World!"

    def test_read_into_stored_binary(self, stored_zip: bytes) -> None:
        reader = MockSyncReader(stored_zip)
        with SyncRemoteZip(reader) as rz:
            dest = io.BytesIO()
            rz.read_into("data/numbers.bin", dest)
        assert dest.getvalue() == b"\x01\x02\x03\x04\x05"

    def test_read_into_deflated(self, deflated_zip: bytes) -> None:
        reader = MockSyncReader(deflated_zip)
        with SyncRemoteZip(reader) as rz:
            dest = io.BytesIO()
            rz.read_into("hello.txt", dest)
        assert dest.getvalue() == b"Hello, World!"

    def test_read_into_deflated_repeated(self, deflated_zip: bytes) -> None:
        reader = MockSyncReader(deflated_zip)
        with SyncRemoteZip(reader) as rz:
            dest = io.BytesIO()
            rz.read_into("repeated.txt", dest)
        assert dest.getvalue() == b"AAAA" * 1000

    def test_read_into_by_info(self, stored_zip: bytes) -> None:
        reader = MockSyncReader(stored_zip)
        with SyncRemoteZip(reader) as rz:
            info = rz.getinfo("hello.txt")
            dest = io.BytesIO()
            rz.read_into(info, dest)
        assert dest.getvalue() == b"Hello, World!"

    def test_read_into_directory(self, directory_zip: bytes) -> None:
        reader = MockSyncReader(directory_zip)
        with SyncRemoteZip(reader) as rz:
            for name in rz.namelist():
                info = rz.getinfo(name)
                if info.is_dir():
                    dest = io.BytesIO()
                    rz.read_into(info, dest)
                    assert dest.getvalue() == b""

    def test_read_into_mixed_compression(self, mixed_zip: bytes) -> None:
        reader = MockSyncReader(mixed_zip)
        with SyncRemoteZip(reader) as rz:
            stored_dest = io.BytesIO()
            rz.read_into("stored.txt", stored_dest)
            assert stored_dest.getvalue() == b"stored content"

            deflated_dest = io.BytesIO()
            rz.read_into("deflated.txt", deflated_dest)
            assert deflated_dest.getvalue() == b"deflated " * 100

    def test_matches_stdlib(self, stored_zip: bytes) -> None:
        """Verify we read the same data as stdlib zipfile."""
        reader = MockSyncReader(stored_zip)
        with SyncRemoteZip(reader) as rz:
            our_data = {name: rz.read(name) for name in rz.namelist()}

        with zipfile.ZipFile(io.BytesIO(stored_zip)) as zf:
            stdlib_data = {name: zf.read(name) for name in zf.namelist()}

        assert our_data == stdlib_data

    def test_read_large_file(self, large_file_zip: bytes) -> None:
        """File > 50 KiB bypasses prefetch, uses separate read_range for data."""
        expected = bytes(range(256)) * 208
        reader = MockSyncReader(large_file_zip)
        with SyncRemoteZip(reader) as rz:
            data = rz.read("large.bin")
        assert data == expected

    def test_read_into_large_file(self, large_file_zip: bytes) -> None:
        """File > 50 KiB uses stream_range for streaming decompression."""
        expected = bytes(range(256)) * 208
        reader = MockSyncReader(large_file_zip)
        with SyncRemoteZip(reader) as rz:
            dest = io.BytesIO()
            rz.read_into("large.bin", dest)
        assert dest.getvalue() == expected

    def test_read_into_bounded_output_chunks(self) -> None:
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
        with SyncRemoteZip(MockSyncReader(archive_data)) as rz:
            rz.read_into("entry.bin", Destination())
        assert bytes(output) == content
        assert max(sizes) <= STREAM_CHUNK_SIZE

    @pytest.mark.parametrize("size", [100, 70_000])
    @pytest.mark.parametrize("write_limit", [None, 3])
    def test_read_into_partial_writes(self, size: int, write_limit: int | None) -> None:
        """Retry the unconsumed suffix of a short write."""
        content = (bytes(range(256)) * (size // 256 + 1))[:size]
        output = bytearray()

        class Destination:
            def write(self, chunk: bytes) -> int | None:
                count = len(chunk) if write_limit is None else min(write_limit, len(chunk))
                output.extend(chunk[:count])
                return None if write_limit is None else count

        reader = MockSyncReader(make_zip({"entry.bin": content}))
        with SyncRemoteZip(reader) as rz:
            rz.read_into("entry.bin", Destination())
        assert bytes(output) == content

    @pytest.mark.parametrize("size", [100, 70_000])
    @pytest.mark.parametrize("failure", ["zero", "negative", "oversized", "noninteger", "boolean"])
    def test_read_into_invalid_write_count(self, size: int, failure: str) -> None:
        """Reject byte counts that would stall or corrupt the write loop."""

        class Destination:
            def write(self, chunk: bytes) -> int | None:
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

        reader = MockSyncReader(make_zip({"entry.bin": b"x" * size}))
        with SyncRemoteZip(reader) as rz, pytest.raises(OSError, match="invalid byte count"):
            rz.read_into("entry.bin", Destination())

    def test_read_into_closes_stream_on_error(self, large_file_zip: bytes) -> None:
        """Close the response generator when writing fails mid-stream."""

        class ClosingReader(MockSyncReader):
            stream_closed = False

            def stream_range(self, offset: int, length: int) -> Generator[bytes, None, None]:
                try:
                    yield from super().stream_range(offset, length)
                finally:
                    self.stream_closed = True

        class FailingDest:
            def write(self, data: bytes) -> None:
                raise OSError("write failed")

        reader = ClosingReader(large_file_zip)
        with SyncRemoteZip(reader) as rz, pytest.raises(OSError, match="write failed"):
            rz.read_into("large.bin", FailingDest())
        assert reader.stream_closed

    def test_read_into_close_error_chains(self, large_file_zip: bytes) -> None:
        """A failing stream close() chains the extraction error as context."""

        class FailingCloseReader(MockSyncReader):
            def stream_range(self, offset: int, length: int) -> Generator[bytes, None, None]:
                try:
                    yield from super().stream_range(offset, length)
                finally:
                    raise OSError("close failed")

        class FailingDest:
            def write(self, data: bytes) -> None:
                raise OSError("write failed")

        reader = FailingCloseReader(large_file_zip)
        with SyncRemoteZip(reader) as rz, pytest.raises(OSError, match="close failed") as excinfo:
            rz.read_into("large.bin", FailingDest())
        # The original error is chained behind the GeneratorExit that
        # close() threw into the generator.
        context = excinfo.value.__context__
        while context is not None and "write failed" not in str(context):
            context = context.__context__
        assert "write failed" in str(context)

    def test_read_zstandard(self, zstandard_zip: bytes) -> None:
        reader = MockSyncReader(zstandard_zip)
        with SyncRemoteZip(reader) as rz:
            assert rz.read("hello.txt") == b"Hello, World!"
            assert rz.read("repeated.txt") == b"AAAA" * 1000

    def test_read_into_zstandard(self, zstandard_zip: bytes) -> None:
        reader = MockSyncReader(zstandard_zip)
        with SyncRemoteZip(reader) as rz:
            dest = io.BytesIO()
            rz.read_into("hello.txt", dest)
            assert dest.getvalue() == b"Hello, World!"

            dest = io.BytesIO()
            rz.read_into("repeated.txt", dest)
            assert dest.getvalue() == b"AAAA" * 1000

    @pytest.mark.parametrize("method", ["read", "read_into"])
    def test_file_too_large(self, stored_zip: bytes, method: str) -> None:
        reader = MockSyncReader(stored_zip)
        with SyncRemoteZip(reader) as rz, pytest.raises(FileTooLarge):
            if method == "read":
                rz.read("hello.txt", max_file_size=1)
            else:
                rz.read_into("hello.txt", io.BytesIO(), max_file_size=1)

    def test_no_content_length(self, stored_zip: bytes) -> None:
        reader = MockSyncReader(stored_zip)
        reader.head = lambda: {"accept-ranges": "bytes"}
        with pytest.raises(RangeRequestUnsupported, match="Content-Length"), SyncRemoteZip(reader):
            pass


def _cd_entry_offset(data: bytes, index: int = 0) -> int:
    """Absolute offset of central directory entry *index* in *data*."""
    eocd = find_eocd(data, len(data))
    offset = eocd.cd_offset
    for _ in range(index):
        fn_len, extra_len, comment_len = struct.unpack_from("<HHH", data, offset + 28)
        offset += CENTRAL_DIR_SIZE + fn_len + extra_len + comment_len
    return offset


def _or_cd_flags(data: bytes, mask: int, index: int = 0) -> bytes:
    """Set general purpose flag bits in central directory entry *index*."""
    buf = bytearray(data)
    offset = _cd_entry_offset(data, index)
    flags = struct.unpack_from("<H", buf, offset + 8)[0]
    struct.pack_into("<H", buf, offset + 8, flags | mask)
    return bytes(buf)


class ShortTailReader(MockSyncReader):
    """Reader whose first (tail) read_range returns one byte too few."""

    def read_range(
        self,
        offset: int,
        length: int,
    ) -> tuple[bytes, dict[str, str]]:
        data, headers = super().read_range(offset, length)
        if self.read_count == 1:
            data = data[:-1]
        return data, headers


class ShortCdReader(MockSyncReader):
    """Reader whose second (central directory) read_range is short."""

    def read_range(
        self,
        offset: int,
        length: int,
    ) -> tuple[bytes, dict[str, str]]:
        data, headers = super().read_range(offset, length)
        if self.read_count == 2:
            data = data[:-1]
        return data, headers


class TestHardening:
    """Admission and bounds checks ported from zipfile."""

    def test_encrypted_entry_rejected(self, stored_zip: bytes) -> None:
        data = _or_cd_flags(stored_zip, FLAG_ENCRYPTED)
        reader = MockSyncReader(data)
        with SyncRemoteZip(reader) as rz, pytest.raises(UnsupportedFeature, match="encrypted"):
            rz.read("hello.txt")

    def test_patched_data_rejected(self, stored_zip: bytes) -> None:
        data = _or_cd_flags(stored_zip, FLAG_COMPRESSED_PATCHED_DATA)
        reader = MockSyncReader(data)
        with SyncRemoteZip(reader) as rz, pytest.raises(UnsupportedFeature, match="patched data"):
            rz.read("hello.txt")

    def test_strong_encryption_rejected(self, stored_zip: bytes) -> None:
        data = _or_cd_flags(stored_zip, FLAG_STRONG_ENCRYPTION)
        reader = MockSyncReader(data)
        with (
            SyncRemoteZip(reader) as rz,
            pytest.raises(UnsupportedFeature, match="strong encryption"),
        ):
            rz.read("hello.txt")

    def test_encrypted_entry_rejected_by_read_into(self, stored_zip: bytes) -> None:
        data = _or_cd_flags(stored_zip, FLAG_ENCRYPTED)
        reader = MockSyncReader(data)
        with SyncRemoteZip(reader) as rz, pytest.raises(UnsupportedFeature, match="encrypted"):
            rz.read_into("hello.txt", io.BytesIO())

    def test_header_offset_beyond_archive(self, stored_zip: bytes) -> None:
        buf = bytearray(stored_zip)
        offset = _cd_entry_offset(stored_zip, 0)
        struct.pack_into("<I", buf, offset + 42, len(stored_zip) + 100)
        reader = MockSyncReader(bytes(buf))
        with (
            SyncRemoteZip(reader) as rz,
            pytest.raises(BadZipFile, match="beyond the end of the archive"),
        ):
            rz.read("hello.txt")

    def test_data_beyond_archive(self, stored_zip: bytes) -> None:
        # Inflate compress_size so the data ends past the last byte of
        # the archive.
        buf = bytearray(stored_zip)
        offset = _cd_entry_offset(stored_zip, 0)
        struct.pack_into("<I", buf, offset + 20, len(stored_zip))
        reader = MockSyncReader(bytes(buf))
        with SyncRemoteZip(reader) as rz, pytest.raises(BadZipFile, match="beyond the end"):
            rz.read("hello.txt")

    def test_overlapped_entry_rejected(self, stored_zip: bytes) -> None:
        # Inflate compress_size so the data runs past the next entry's
        # local header, but stays inside the archive.
        buf = bytearray(stored_zip)
        offset = _cd_entry_offset(stored_zip, 0)
        struct.pack_into("<I", buf, offset + 20, 100)
        reader = MockSyncReader(bytes(buf))
        with SyncRemoteZip(reader) as rz, pytest.raises(BadZipFile, match="Overlapped entries"):
            rz.read("hello.txt")

    def test_overlapped_entry_rejected_by_read_into(self, stored_zip: bytes) -> None:
        buf = bytearray(stored_zip)
        offset = _cd_entry_offset(stored_zip, 0)
        struct.pack_into("<I", buf, offset + 20, 100)
        reader = MockSyncReader(bytes(buf))
        with (
            SyncRemoteZip(reader) as rz,
            pytest.raises(BadZipFile, match="Overlapped entries"),
        ):
            rz.read_into("hello.txt", io.BytesIO())

    def test_shared_local_header_warns(self) -> None:
        # Two entries pointing at the same local header: zipfile warns
        # (rather than raising) and still reads the data.
        data = make_zip({"one.txt": b"same data", "two.txt": b"same data"})
        buf = bytearray(data)
        first_hoff = struct.unpack_from("<I", buf, _cd_entry_offset(data, 0) + 42)[0]
        struct.pack_into("<I", buf, _cd_entry_offset(data, 1) + 42, first_hoff)
        reader = MockSyncReader(bytes(buf))
        with (
            SyncRemoteZip(reader) as rz,
            pytest.warns(UserWarning, match="Overlapped entries") as caught,
        ):
            assert rz.read("two.txt") == b"same data"
        # The warning is attributed to the caller, not to zipwire.
        assert caught[0].filename == __file__

    def test_cd_bounds_checked_before_fetch(self, stored_zip: bytes) -> None:
        # A hostile cd_size must not become a range request: only the
        # initial tail fetch is allowed before the archive is rejected.
        buf = bytearray(stored_zip)
        eocd_pos = buf.rfind(b"PK\x05\x06")
        struct.pack_into("<I", buf, eocd_pos + 12, 0xFFFFFF00)
        data = bytes(buf)
        reader = MockSyncReader(data)
        with (
            pytest.raises(BadZipFile, match="beyond the end of the archive"),
            SyncRemoteZip(reader),
        ):
            pass
        assert reader.requests == [(0, len(data))]

    def test_prefetch_fetch_stays_in_bounds(self, stored_zip: bytes) -> None:
        # A lying compress_size below the prefetch threshold must not
        # become a range request past the end of the archive either.
        buf = bytearray(stored_zip)
        eocd = find_eocd(stored_zip, len(stored_zip))
        struct.pack_into("<I", buf, eocd.cd_offset + 20, 5000)
        data = bytes(buf)
        reader = MockSyncReader(data)
        with (
            SyncRemoteZip(reader) as rz,
            pytest.raises(BadZipFile, match="beyond the end of the archive"),
        ):
            rz.read("hello.txt")
        assert all(offset + length <= len(data) for offset, length in reader.requests)

    def test_cd_beyond_archive_rejected_on_load(self, stored_zip: bytes) -> None:
        buf = bytearray(stored_zip)
        eocd_pos = buf.rfind(b"PK\x05\x06")
        cd_offset = struct.unpack_from("<I", buf, eocd_pos + 16)[0]
        struct.pack_into("<I", buf, eocd_pos + 16, cd_offset + 1000)
        reader = MockSyncReader(bytes(buf))
        with (
            pytest.raises(BadZipFile, match="beyond the end of the archive"),
            SyncRemoteZip(reader),
        ):
            pass

    def test_short_tail_read_rejected(self, stored_zip: bytes) -> None:
        reader = ShortTailReader(stored_zip)
        with pytest.raises(BadZipFile, match="tail fetch returned"), SyncRemoteZip(reader):
            pass

    def test_short_cd_read_rejected(self, large_file_zip: bytes) -> None:
        reader = ShortCdReader(large_file_zip)
        with (
            pytest.raises(BadZipFile, match="Central directory fetch returned"),
            SyncRemoteZip(reader),
        ):
            pass
