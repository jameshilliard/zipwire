"""Verify RemoteZipInfo is a true subclass of zipfile.ZipInfo."""

from __future__ import annotations

import io
import struct
import zipfile
from zlib import crc32

import pytest

from zipwire._constants import MAX_UINT16, MAX_UINT32, ExtraDataId
from zipwire._errors import BadZipFile
from zipwire._parser import find_eocd, parse_central_directory
from zipwire._zipinfo import RemoteZipInfo
from zipwire._zipstructs import CentralDirHeaderRecord


class TestRemoteZipInfoCompat:
    def _get_infos(self, zip_data: bytes) -> list[RemoteZipInfo]:
        eocd = find_eocd(zip_data, len(zip_data))
        cd_data = zip_data[eocd.cd_offset : eocd.cd_offset + eocd.cd_size]
        entries = list(parse_central_directory(cd_data))
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
        assert "éàü.txt" in filenames
        assert "€100/rates.txt" in filenames

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


def make_cd_entry(
    extra: bytes = b"",
    filename: bytes = b"",
    **overrides: int,
) -> CentralDirHeaderRecord:
    """A central directory header record with overridable fields."""
    values: dict[str, int] = {
        "version_made_by": 45,
        "version_needed": 45,
        "flags": 0,
        "compression_method": 0,
        "mod_time": 0,
        "mod_date": 0,
        "crc32": 0,
        "compressed_size": 10,
        "uncompressed_size": 10,
        "filename_length": 0,
        "extra_length": 0,
        "comment_length": 0,
        "disk_start": 0,
        "internal_attr": 0,
        "external_attr": 0,
        "header_offset": 100,
    }
    values.update(overrides)
    values["filename_length"] = len(filename)
    values["extra_length"] = len(extra)
    header = b"PK\x01\x02" + struct.pack("<HHHHHHIIIHHHHHII", *values.values())
    return CentralDirHeaderRecord.parse(header + filename + extra)


def zip64_extra(payload: bytes) -> bytes:
    """A ZIP64 extended information extra field wrapping *payload*."""
    return struct.pack("<HH", ExtraDataId.ZIP64, len(payload)) + payload


class TestZip64Resolution:
    """``_from_central_dir_entry`` resolves ZIP64 sentinels."""

    def test_zip64_sizes_resolved(self) -> None:
        """Sentinel file_size and compress_size are updated from the field."""
        payload = struct.pack("<QQ", 0x1_0000_0000, 0x1_0000_0001)
        entry = make_cd_entry(
            zip64_extra(payload),
            uncompressed_size=MAX_UINT32,
            compressed_size=MAX_UINT32,
        )
        info = RemoteZipInfo._from_central_dir_entry(entry)
        assert info.file_size == 0x1_0000_0000
        assert info.compress_size == 0x1_0000_0001
        assert info.header_offset == 100  # unchanged

    def test_zip64_all_fields_resolved(self) -> None:
        """All four sentinel fields are updated from the field."""
        payload = struct.pack("<QQQI", 0x2_0000_0000, 0x2_0000_0001, 0x2_0000_0002, 42)
        entry = make_cd_entry(
            zip64_extra(payload),
            uncompressed_size=MAX_UINT32,
            compressed_size=MAX_UINT32,
            header_offset=MAX_UINT32,
            disk_start=MAX_UINT16,
        )
        info = RemoteZipInfo._from_central_dir_entry(entry)
        assert info.file_size == 0x2_0000_0000
        assert info.compress_size == 0x2_0000_0001
        assert info.header_offset == 0x2_0000_0002
        assert info.volume == 42

    def test_zip64_without_sentinels(self) -> None:
        """A ZIP64 field next to other fields updates nothing without sentinels."""
        other = struct.pack("<HH", 0x000A, 8) + b"\x00" * 8
        payload = struct.pack("<Q", 0x3_0000_0000)
        entry = make_cd_entry(other + zip64_extra(payload))
        info = RemoteZipInfo._from_central_dir_entry(entry)
        assert info.file_size == 10  # unchanged (not sentinel)
        assert info.compress_size == 10
        assert info.header_offset == 100

    def test_zip64_value_missing(self) -> None:
        """A sentinel promises a ZIP64 value that the field does not hold."""
        entry = make_cd_entry(
            zip64_extra(b"\x00" * 4),
            uncompressed_size=MAX_UINT32,
        )
        with pytest.raises(BadZipFile, match="File size not found"):
            RemoteZipInfo._from_central_dir_entry(entry)


def test_unicode_path_extra_field() -> None:
    """A version 1 Unicode Path field with a matching CRC-32 renames the entry."""
    raw = b"cafe.txt"
    payload = struct.pack("<BI", 1, crc32(raw)) + "café.txt".encode()
    extra = struct.pack("<HH", ExtraDataId.UNICODE_PATH, len(payload)) + payload
    info = RemoteZipInfo._from_central_dir_entry(make_cd_entry(extra, filename=raw))
    assert info.filename == "café.txt"
    assert info.orig_filename == "cafe.txt"


def test_unicode_path_null_byte_rejected() -> None:
    """A null byte in a Unicode Path field name is rejected."""
    raw = b"cafe.txt"
    payload = struct.pack("<BI", 1, crc32(raw)) + "café.txt\0evil.txt".encode()
    extra = struct.pack("<HH", ExtraDataId.UNICODE_PATH, len(payload)) + payload
    with pytest.raises(BadZipFile, match="null byte"):
        RemoteZipInfo._from_central_dir_entry(make_cd_entry(extra, filename=raw))
