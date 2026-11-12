"""Tests for SyncRemoteZip with MockSyncReader."""

from __future__ import annotations

import io
import zipfile

import pytest

from tests.conftest import MockSyncReader, make_zip
from zipwire import EOCDInfo, SyncRemoteZip
from zipwire._constants import STREAM_CHUNK_SIZE
from zipwire._errors import FileNotFoundInZip, FileTooLarge, RangeRequestUnsupported
from zipwire._zipinfo import RemoteZipInfo


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
