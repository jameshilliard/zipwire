"""Verify RemoteZipInfo is a true subclass of zipfile.ZipInfo."""

from __future__ import annotations

import io
import struct
import zipfile

from zipwire._constants import ZIP64_EXTRA_FIELD_ID
from zipwire._parser import find_eocd, parse_central_directory
from zipwire._zipinfo import RemoteZipInfo, _apply_extra_fields


class TestRemoteZipInfoCompat:
    def _get_infos(self, zip_data: bytes) -> list[RemoteZipInfo]:
        eocd = find_eocd(zip_data, len(zip_data))
        cd_data = zip_data[eocd.cd_offset : eocd.cd_offset + eocd.cd_size]
        entries = parse_central_directory(cd_data)
        return [RemoteZipInfo._from_central_dir_entry(e) for e in entries]

    def test_isinstance(self, stored_zip: bytes) -> None:
        infos = self._get_infos(stored_zip)
        for info in infos:
            assert isinstance(info, zipfile.ZipInfo)
            assert isinstance(info, RemoteZipInfo)

    def test_filename(self, stored_zip: bytes) -> None:
        infos = self._get_infos(stored_zip)
        filenames = [info.filename for info in infos]
        assert "hello.txt" in filenames
        assert "data/numbers.bin" in filenames

    def test_date_time(self, stored_zip: bytes) -> None:
        infos = self._get_infos(stored_zip)
        for info in infos:
            assert len(info.date_time) == 6
            year, month, day, _hour, _minute, _second = info.date_time
            assert 1980 <= year <= 2107
            assert 1 <= month <= 12
            assert 1 <= day <= 31

    def test_compress_type(self, stored_zip: bytes) -> None:
        infos = self._get_infos(stored_zip)
        for info in infos:
            assert info.compress_type == 0  # STORED

    def test_deflated_compress_type(self, deflated_zip: bytes) -> None:
        infos = self._get_infos(deflated_zip)
        for info in infos:
            assert info.compress_type == 8  # DEFLATED

    def test_sizes(self, stored_zip: bytes) -> None:
        infos = self._get_infos(stored_zip)
        hello = next(i for i in infos if i.filename == "hello.txt")
        assert hello.file_size == 13  # len("Hello, World!")
        assert hello.compress_size == 13  # STORED, same size

    def test_crc(self, stored_zip: bytes) -> None:
        infos = self._get_infos(stored_zip)
        for info in infos:
            assert info.CRC != 0

    def test_is_dir_false(self, stored_zip: bytes) -> None:
        infos = self._get_infos(stored_zip)
        for info in infos:
            assert not info.is_dir()

    def test_is_dir_true(self, directory_zip: bytes) -> None:
        infos = self._get_infos(directory_zip)
        dir_info = next(i for i in infos if i.filename.endswith("/") and "file" not in i.filename)
        assert dir_info.is_dir()

    def test_unicode_filenames(self, unicode_zip: bytes) -> None:
        infos = self._get_infos(unicode_zip)
        filenames = [info.filename for info in infos]
        assert "\u00e9\u00e0\u00fc.txt" in filenames
        assert "\u4f60\u597d/world.txt" in filenames

    def test_header_offset(self, stored_zip: bytes) -> None:
        infos = self._get_infos(stored_zip)
        for info in infos:
            assert info.header_offset >= 0

    def test_external_attr(self, stored_zip: bytes) -> None:
        infos = self._get_infos(stored_zip)
        for info in infos:
            assert hasattr(info, "external_attr")

    def test_matches_stdlib(self, stored_zip: bytes) -> None:
        """Verify our RemoteZipInfo matches stdlib's parsing of the same ZIP."""
        our_infos = self._get_infos(stored_zip)
        with zipfile.ZipFile(io.BytesIO(stored_zip)) as zf:
            stdlib_infos = zf.infolist()

        assert len(our_infos) == len(stdlib_infos)
        for ours, stdlib in zip(our_infos, stdlib_infos, strict=True):
            assert ours.filename == stdlib.filename
            assert ours.compress_type == stdlib.compress_type
            assert ours.CRC == stdlib.CRC
            assert ours.file_size == stdlib.file_size
            assert ours.compress_size == stdlib.compress_size
            assert ours.header_offset == stdlib.header_offset


class TestZip64ExtraField:
    def test_zip64_extra_sizes(self) -> None:
        """Sentinel file_size and compress_size are updated from ZIP64 extra field."""
        info = RemoteZipInfo("test.txt")
        info.file_size = 0xFFFFFFFF
        info.compress_size = 0xFFFFFFFF
        info.header_offset = 100
        info.volume = 0

        real_file_size = 0x1_0000_0000
        real_compress_size = 0x1_0000_0001
        extra_data = struct.pack("<Q", real_file_size) + struct.pack("<Q", real_compress_size)
        info.extra = struct.pack("<HH", ZIP64_EXTRA_FIELD_ID, len(extra_data)) + extra_data

        _apply_extra_fields(info, b"test.txt")
        assert info.file_size == real_file_size
        assert info.compress_size == real_compress_size
        assert info.header_offset == 100  # unchanged

    def test_zip64_extra_all_fields(self) -> None:
        """Sizes and offset are updated; the ZIP64 disk number is ignored."""
        info = RemoteZipInfo("test.txt")
        info.file_size = 0xFFFFFFFF
        info.compress_size = 0xFFFFFFFF
        info.header_offset = 0xFFFFFFFF
        info.volume = 0xFFFF

        real_file_size = 0x2_0000_0000
        real_compress_size = 0x2_0000_0001
        real_header_offset = 0x2_0000_0002
        real_volume = 42
        extra_data = (
            struct.pack("<Q", real_file_size)
            + struct.pack("<Q", real_compress_size)
            + struct.pack("<Q", real_header_offset)
            + struct.pack("<I", real_volume)
        )
        info.extra = struct.pack("<HH", ZIP64_EXTRA_FIELD_ID, len(extra_data)) + extra_data

        _apply_extra_fields(info, b"test.txt")
        assert info.file_size == real_file_size
        assert info.compress_size == real_compress_size
        assert info.header_offset == real_header_offset
        assert info.volume == 0xFFFF

    def test_zip64_extra_with_other_fields(self) -> None:
        """Non-ZIP64 extra field preceding the ZIP64 field."""
        info = RemoteZipInfo("test.txt")
        info.file_size = 0xFFFFFFFF
        info.compress_size = 500
        info.header_offset = 100
        info.volume = 0

        # Some other extra field first (id=0x000a, 8 bytes of data)
        other_extra = struct.pack("<HH", 0x000A, 8) + b"\x00" * 8

        # ZIP64 extra field with just file_size (compress_size is not sentinel)
        real_file_size = 0x3_0000_0000
        zip64_data = struct.pack("<Q", real_file_size)
        zip64_extra = struct.pack("<HH", ZIP64_EXTRA_FIELD_ID, len(zip64_data)) + zip64_data

        info.extra = other_extra + zip64_extra

        _apply_extra_fields(info, b"test.txt")
        assert info.file_size == real_file_size
        assert info.compress_size == 500  # unchanged (not sentinel)
