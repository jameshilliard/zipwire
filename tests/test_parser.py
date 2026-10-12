"""Unit tests for EOCD, central directory, and local header parsing."""

from __future__ import annotations

import struct

import pytest

from zipwire._constants import (
    CENTRAL_DIR_SIZE,
    EOCD_STRUCT,
    LOCAL_FILE_HEADER_SIZE,
    ZIP64_EOCD_LOCATOR_STRUCT,
    ZIP64_EOCD_STRUCT,
)
from zipwire._errors import BadZipFile
from zipwire._parser import (
    EOCDInfo,
    find_eocd,
    parse_central_directory,
    parse_local_file_header,
)


class TestFindEocd:
    def test_simple_zip(self, stored_zip: bytes) -> None:
        tail = stored_zip
        eocd = find_eocd(tail, len(stored_zip))
        assert isinstance(eocd, EOCDInfo)
        assert eocd.cd_entry_count == 2
        assert eocd.cd_size > 0
        assert eocd.cd_offset >= 0

    def test_empty_zip(self, empty_zip: bytes) -> None:
        eocd = find_eocd(empty_zip, len(empty_zip))
        assert eocd.cd_entry_count == 0

    def test_zip_with_comment(self, comment_zip: bytes) -> None:
        eocd = find_eocd(comment_zip, len(comment_zip))
        assert eocd.cd_entry_count == 1

    def test_no_eocd_signature(self) -> None:
        with pytest.raises(BadZipFile, match="Could not find"):
            find_eocd(b"\x00" * 100, 100)

    def test_truncated_eocd(self) -> None:
        # EOCD signature but not enough bytes after it
        data = b"PK\x05\x06" + b"\x00" * 10  # Only 14 bytes, need 22
        with pytest.raises(BadZipFile, match="truncated"):
            find_eocd(data, len(data))

    @pytest.mark.parametrize("field", [5, 6])
    def test_signature_in_eocd_field(self, field: int) -> None:
        signature_value = int.from_bytes(b"PK\x05\x06", "little")
        cd_size = signature_value if field == 5 else 50
        cd_offset = signature_value if field == 6 else 100
        record = EOCD_STRUCT.pack(b"PK\x05\x06", 0, 0, 1, 1, cd_size, cd_offset, 0)
        tail = b"\0" * 20 + record
        file_size = cd_offset + cd_size + len(record)
        info = find_eocd(tail, file_size)
        assert info.cd_size == cd_size
        assert info.cd_offset == cd_offset
        assert info.archive_offset == 0
        assert info.comment == b""

    def test_truncated_comment(self) -> None:
        data = EOCD_STRUCT.pack(b"PK\x05\x06", 0, 0, 0, 0, 0, 0, 10) + b"short"
        assert find_eocd(data, len(data)).comment == b"short"

    def test_zip64_eocd(self) -> None:
        """Construct a tail with ZIP64 EOCD Record + Locator + EOCD."""
        cd_entry_count = 5
        cd_size = 1000
        cd_offset = 2000

        # ZIP64 EOCD Record (56 bytes)
        zip64_eocd = ZIP64_EOCD_STRUCT.pack(
            b"PK\x06\x06",  # signature
            44,  # record size (remaining bytes after this field)
            45,  # version made by
            45,  # version needed
            0,  # disk number
            0,  # disk with CD start
            cd_entry_count,  # entries this disk
            cd_entry_count,  # entries total
            cd_size,  # CD size
            cd_offset,  # CD offset
        )

        # The ZIP64 EOCD record is at the beginning of our tail
        # Its absolute offset in the file = tail_start + 0
        file_size = cd_offset + cd_size + len(zip64_eocd) + 20 + 22
        tail_start = file_size - (len(zip64_eocd) + 20 + 22)  # zip64_eocd + locator + eocd
        zip64_eocd_abs_offset = tail_start

        # ZIP64 EOCD Locator (20 bytes)
        locator = ZIP64_EOCD_LOCATOR_STRUCT.pack(
            b"PK\x06\x07",  # signature
            0,  # disk with ZIP64 EOCD
            zip64_eocd_abs_offset,  # absolute offset of ZIP64 EOCD
            1,  # total disks
        )

        # Regular EOCD with sentinel values (22 bytes)
        eocd = EOCD_STRUCT.pack(
            b"PK\x05\x06",
            0,  # disk number
            0,  # disk with CD start
            0xFFFF,  # entries this disk (sentinel)
            0xFFFF,  # entries total (sentinel)
            0xFFFFFFFF,  # CD size (sentinel)
            0xFFFFFFFF,  # CD offset (sentinel)
            0,  # comment length
        )

        tail = zip64_eocd + locator + eocd
        result = find_eocd(tail, file_size)
        assert isinstance(result, EOCDInfo)
        assert result.cd_entry_count == cd_entry_count
        assert result.cd_size == cd_size
        assert result.cd_offset == cd_offset

    def test_zip64_locator_not_found(self) -> None:
        """Not enough data before EOCD for the locator."""
        # Regular EOCD with sentinel triggering ZIP64 path, but only EOCD data
        eocd = EOCD_STRUCT.pack(
            b"PK\x05\x06",
            0,
            0,
            0xFFFF,
            0xFFFF,
            0xFFFFFFFF,
            0xFFFFFFFF,
            0,
        )
        # tail is exactly the EOCD - no room for locator before it
        with pytest.raises(BadZipFile, match="central directory offset"):
            find_eocd(eocd, len(eocd))

    def test_zip64_locator_bad_signature(self) -> None:
        """Locator position exists but has wrong signature."""
        eocd = EOCD_STRUCT.pack(
            b"PK\x05\x06",
            0,
            0,
            0xFFFF,
            0xFFFF,
            0xFFFFFFFF,
            0xFFFFFFFF,
            0,
        )
        # 20 bytes of zeros before EOCD (wrong locator signature)
        tail = b"\x00" * 20 + eocd
        with pytest.raises(BadZipFile, match="central directory offset"):
            find_eocd(tail, len(tail))

    def test_zip64_eocd_outside_tail(self) -> None:
        """Locator points to a ZIP64 EOCD offset outside the fetched tail."""
        locator = ZIP64_EOCD_LOCATOR_STRUCT.pack(
            b"PK\x06\x07",
            0,
            0,  # absurd offset (0, but tail_start will be > 0)
            1,
        )
        eocd = EOCD_STRUCT.pack(
            b"PK\x05\x06",
            0,
            0,
            0xFFFF,
            0xFFFF,
            0xFFFFFFFF,
            0xFFFFFFFF,
            0,
        )
        tail = locator + eocd
        # file_size much larger than tail, so zip64_eocd_rel will be negative
        with pytest.raises(BadZipFile, match="outside"):
            find_eocd(tail, 1_000_000)

    def test_zip64_eocd_bad_signature(self) -> None:
        """ZIP64 EOCD record exists at expected position but has wrong signature."""
        file_size = 200
        # Fake ZIP64 EOCD area (56 bytes of zeros - wrong signature)
        fake_zip64_eocd = b"\x00" * 56
        fake_zip64_eocd_abs_offset = file_size - (56 + 20 + 22)

        locator = ZIP64_EOCD_LOCATOR_STRUCT.pack(
            b"PK\x06\x07",
            0,
            fake_zip64_eocd_abs_offset,
            1,
        )
        eocd = EOCD_STRUCT.pack(
            b"PK\x05\x06",
            0,
            0,
            0xFFFF,
            0xFFFF,
            0xFFFFFFFF,
            0xFFFFFFFF,
            0,
        )
        tail = fake_zip64_eocd + locator + eocd
        with pytest.raises(BadZipFile, match="record signature"):
            find_eocd(tail, file_size)


class TestParseCentralDirectory:
    def test_stored_zip(self, stored_zip: bytes) -> None:
        eocd = find_eocd(stored_zip, len(stored_zip))
        cd_data = stored_zip[eocd.cd_offset : eocd.cd_offset + eocd.cd_size]
        entries = list(parse_central_directory(cd_data))
        assert len(entries) == 2
        filenames = [e.filename for e in entries]
        assert b"hello.txt" in filenames
        assert b"data/numbers.bin" in filenames

    def test_entry_fields(self, stored_zip: bytes) -> None:
        eocd = find_eocd(stored_zip, len(stored_zip))
        cd_data = stored_zip[eocd.cd_offset : eocd.cd_offset + eocd.cd_size]
        entries = parse_central_directory(cd_data)

        hello_entry = next(e for e in entries if e.filename == b"hello.txt")
        assert hello_entry.compression_method == 0  # STORED
        assert hello_entry.uncompressed_size == 13  # len("Hello, World!")
        assert hello_entry.crc32 != 0
        assert hello_entry.header_offset >= 0

    def test_truncated_directory(self) -> None:
        # Central dir signature but truncated
        data = b"PK\x01\x02" + b"\x00" * 10
        with pytest.raises(BadZipFile, match="truncated"):
            list(parse_central_directory(data))

    def test_bad_signature(self) -> None:
        data = b"\x00" * CENTRAL_DIR_SIZE
        with pytest.raises(BadZipFile, match="signature"):
            list(parse_central_directory(data))

    def test_entry_past_end_of_data(self) -> None:
        """CD entry with filename_length=100 but only CENTRAL_DIR_SIZE bytes of data."""
        # Build a valid CD header with a filename_length that exceeds remaining data
        header = struct.pack(
            "<4sHHHHHHIIIHHHHHII",
            b"PK\x01\x02",  # signature
            0,  # version_made_by
            0,  # version_needed
            0,  # flags
            0,  # compression
            0,  # mod_time
            0,  # mod_date
            0,  # crc32
            0,  # compressed_size
            0,  # uncompressed_size
            100,  # filename_length (but no filename data follows)
            0,  # extra_length
            0,  # comment_length
            0,  # disk_start
            0,  # internal_attr
            0,  # external_attr
            0,  # header_offset
        )
        entries = list(parse_central_directory(header))
        assert len(entries) == 1
        assert entries[0].filename == b""


class TestParseLocalFileHeader:
    def test_stored_zip(self, stored_zip: bytes) -> None:
        # The first local header is at offset 0
        header = parse_local_file_header(stored_zip[:LOCAL_FILE_HEADER_SIZE])
        assert header.filename_length == len("hello.txt")
        assert header.data_offset_past_header >= header.filename_length

    def test_truncated_header(self) -> None:
        with pytest.raises(BadZipFile, match="truncated"):
            parse_local_file_header(b"\x00" * 10)

    def test_bad_signature(self) -> None:
        with pytest.raises(BadZipFile, match="signature"):
            parse_local_file_header(b"\x00" * LOCAL_FILE_HEADER_SIZE)
