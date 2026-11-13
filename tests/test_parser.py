"""Tests for EOCD search and central directory walking.

Record-level parsing (truncation, signatures, variable parts) is
covered by ``test_zipstructs.py``; these tests cover the orchestration
in ``zipwire._parser`` on top of the records.
"""

from __future__ import annotations

import struct

import pytest

from zipwire._errors import BadZipFile
from zipwire._parser import EOCDInfo, find_eocd, parse_central_directory


def _make_zip64_tail(
    *,
    cd_size: int = 1000,
    cd_entry_count: int = 5,
    record_size: int = 44,
    locator_disk: int = 0,
    total_disks: int = 1,
    cd_gap: int = 0,
    comment: bytes = b"",
) -> tuple[bytes, int]:
    """Build a ``(tail, file_size)`` pair with ZIP64 EOCD + locator + EOCD.

    The central directory ends exactly where the ZIP64 EOCD record begins,
    unless *cd_gap* shifts its start further back.
    """
    file_size = 5000
    # record + locator + eocd + comment make up the whole tail
    tail_start = file_size - (56 + 20 + 22 + len(comment))
    cd_offset = tail_start - cd_size - cd_gap

    # ZIP64 EOCD Record (56 bytes)
    zip64_eocd = struct.pack(
        "<4sQHHIIQQQQ",
        b"PK\x06\x06",  # signature
        record_size,  # record size (remaining bytes after this field)
        45,  # version made by
        45,  # version needed
        0,  # disk number
        0,  # disk with CD start
        cd_entry_count,  # entries this disk
        cd_entry_count,  # entries total
        cd_size,  # CD size
        cd_offset,  # CD offset
    )

    # ZIP64 EOCD Locator (20 bytes)
    locator = struct.pack(
        "<4sIQI",
        b"PK\x06\x07",  # signature
        locator_disk,  # disk with ZIP64 EOCD
        tail_start,  # absolute offset of ZIP64 EOCD
        total_disks,  # total disks
    )

    # Regular EOCD with sentinel values (22 bytes)
    eocd = struct.pack(
        "<4sHHHHIIH",
        b"PK\x05\x06",
        0,  # disk number
        0,  # disk with CD start
        0xFFFF,  # entries this disk (sentinel)
        0xFFFF,  # entries total (sentinel)
        0xFFFFFFFF,  # CD size (sentinel)
        0xFFFFFFFF,  # CD offset (sentinel)
        len(comment),  # comment length
    )

    return zip64_eocd + locator + eocd + comment, file_size


def _sentinel_eocd() -> bytes:
    """A regular EOCD record with ZIP64 sentinel values."""
    return struct.pack(
        "<4sHHHHIIH",
        b"PK\x05\x06",
        0,
        0,
        0xFFFF,
        0xFFFF,
        0xFFFFFFFF,
        0xFFFFFFFF,
        0,
    )


class TestFindEocd:
    def test_simple_zip(self, stored_zip: bytes) -> None:
        tail = stored_zip
        eocd = find_eocd(tail, len(stored_zip))
        assert isinstance(eocd, EOCDInfo)
        assert eocd.cd_entry_count == 2
        assert eocd.cd_size > 0
        assert eocd.cd_offset >= 0
        assert eocd.comment == b""

    def test_empty_zip(self, empty_zip: bytes) -> None:
        eocd = find_eocd(empty_zip, len(empty_zip))
        assert eocd.cd_entry_count == 0

    def test_zip_with_comment(self, comment_zip: bytes) -> None:
        eocd = find_eocd(comment_zip, len(comment_zip))
        assert eocd.cd_entry_count == 1
        assert eocd.comment == b"This is a ZIP comment"

    def test_trailing_data_tolerated(self, stored_zip: bytes) -> None:
        """Data appended after the EOCD is ignored, as in ``zipfile``."""
        tail = stored_zip + b"appended JUNK"
        eocd = find_eocd(tail, len(tail))
        assert eocd.cd_entry_count == 2
        assert eocd.comment == b""

    def test_no_eocd_signature(self) -> None:
        with pytest.raises(BadZipFile, match="Could not find"):
            find_eocd(b"\x00" * 100, 100)

    def test_zip64_eocd(self) -> None:
        """Construct a tail with ZIP64 EOCD Record + Locator + EOCD."""
        tail, file_size = _make_zip64_tail()
        result = find_eocd(tail, file_size)
        assert isinstance(result, EOCDInfo)
        assert result.cd_entry_count == 5
        assert result.cd_size == 1000
        assert result.cd_offset == file_size - 98 - 1000
        assert result.comment == b""

    def test_zip64_eocd_with_comment(self) -> None:
        """The .ZIP file comment survives the ZIP64 path (classic record only)."""
        tail, file_size = _make_zip64_tail(comment=b"zip64 comment")
        result = find_eocd(tail, file_size)
        assert result.cd_entry_count == 5
        assert result.comment == b"zip64 comment"

    def test_zip64_locator_multiple_disks(self) -> None:
        """Locator claiming the ZIP64 EOCD is on another disk."""
        tail, file_size = _make_zip64_tail(locator_disk=1)
        with pytest.raises(BadZipFile, match="multiple disks"):
            find_eocd(tail, file_size)

    def test_zip64_total_disks(self) -> None:
        """Locator claiming more than one disk."""
        tail, file_size = _make_zip64_tail(total_disks=2)
        with pytest.raises(BadZipFile, match="multiple disks"):
            find_eocd(tail, file_size)

    def test_zip64_record_size_mismatch(self) -> None:
        """Record size field does not match the space before the locator."""
        tail, file_size = _make_zip64_tail(record_size=45)
        with pytest.raises(BadZipFile, match="Corrupt zip64 end of central directory record"):
            find_eocd(tail, file_size)

    def test_zip64_cd_gap_before_record(self) -> None:
        """Central directory does not end where the ZIP64 EOCD record begins."""
        tail, file_size = _make_zip64_tail(cd_gap=10)
        with pytest.raises(BadZipFile, match="Corrupt zip64 end of central directory record"):
            find_eocd(tail, file_size)

    def test_zip64_record_after_locator(self) -> None:
        """Locator points at a record that would start after the locator."""
        file_size = 5000
        tail_start = file_size - (56 + 20 + 22)
        locator = struct.pack(
            "<4sIQI",
            b"PK\x06\x07",
            0,
            tail_start + 60,  # record would overlap the locator and EOCD
            1,
        )
        tail = b"\x00" * 56 + locator + _sentinel_eocd()
        with pytest.raises(BadZipFile, match="Corrupt zip64 end of central directory locator"):
            find_eocd(tail, file_size)

    def test_zip64_locator_not_found(self) -> None:
        """Not enough data before EOCD for the locator."""
        # Regular EOCD with sentinel triggering ZIP64 path, but only EOCD data
        eocd = _sentinel_eocd()
        # tail is exactly the EOCD - no room for locator before it
        with pytest.raises(BadZipFile, match="locator not found"):
            find_eocd(eocd, len(eocd))

    def test_zip64_eocd_outside_tail(self) -> None:
        """Locator points to a ZIP64 EOCD offset outside the fetched tail."""
        locator = struct.pack(
            "<4sIQI",
            b"PK\x06\x07",
            0,
            0,  # absurd offset (0, but tail_start will be > 0)
            1,
        )
        tail = locator + _sentinel_eocd()
        # file_size much larger than tail, so zip64_eocd_rel will be negative
        with pytest.raises(BadZipFile, match="outside"):
            find_eocd(tail, 1_000_000)


class TestParseCentralDirectory:
    def test_stored_zip(self, stored_zip: bytes) -> None:
        eocd = find_eocd(stored_zip, len(stored_zip))
        cd_data = stored_zip[eocd.cd_offset : eocd.cd_offset + eocd.cd_size]
        entries = parse_central_directory(cd_data, eocd.cd_entry_count)
        assert len(entries) == 2
        filenames = [e.filename for e in entries]
        assert b"hello.txt" in filenames
        assert b"data/numbers.bin" in filenames

    def test_entry_fields(self, stored_zip: bytes) -> None:
        eocd = find_eocd(stored_zip, len(stored_zip))
        cd_data = stored_zip[eocd.cd_offset : eocd.cd_offset + eocd.cd_size]
        entries = parse_central_directory(cd_data, eocd.cd_entry_count)

        hello_entry = next(e for e in entries if e.filename == b"hello.txt")
        assert hello_entry.compression_method == 0  # STORED
        assert hello_entry.uncompressed_size == 13  # len("Hello, World!")
        assert hello_entry.crc32 != 0
        assert hello_entry.header_offset >= 0
