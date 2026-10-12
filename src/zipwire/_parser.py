"""Pure ZIP parsing: EOCD, central directory, and local file header.

All functions operate on `bytes` - no IO. This module is shared by both
the sync and async code paths.
"""

from __future__ import annotations

import typing
from dataclasses import dataclass

from zipwire._constants import (
    CENTRAL_DIR_SIGNATURE,
    CENTRAL_DIR_SIZE,
    CENTRAL_DIR_STRUCT,
    EOCD_SIGNATURE,
    EOCD_SIZE,
    EOCD_STRUCT,
    LOCAL_FILE_HEADER_SIGNATURE,
    LOCAL_FILE_HEADER_SIZE,
    LOCAL_FILE_HEADER_STRUCT,
    ZIP64_EOCD_LOCATOR_SIGNATURE,
    ZIP64_EOCD_LOCATOR_SIZE,
    ZIP64_EOCD_LOCATOR_STRUCT,
    ZIP64_EOCD_SIGNATURE,
    ZIP64_EOCD_SIZE,
    ZIP64_EOCD_STRUCT,
)
from zipwire._errors import BadZipFile

if typing.TYPE_CHECKING:
    from collections.abc import Iterator, Mapping


@dataclass(frozen=True, slots=True)
class EOCDInfo:
    """Parsed End of Central Directory information."""

    cd_offset: int
    cd_size: int
    cd_entry_count: int
    archive_offset: int = 0
    comment: bytes = b""


@dataclass(frozen=True, slots=True)
class CentralDirEntry:
    """Raw parsed central directory entry."""

    version_made_by: int
    version_needed: int
    flags: int
    compression_method: int
    mod_time: int
    mod_date: int
    crc32: int
    compressed_size: int
    uncompressed_size: int
    disk_start: int
    internal_attr: int
    external_attr: int
    header_offset: int
    filename: bytes
    extra: bytes
    comment: bytes


@dataclass(frozen=True, slots=True)
class LocalHeaderInfo:
    """Parsed local file header - just enough to find the data offset."""

    filename_length: int
    extra_length: int
    flags: int = 0

    @property
    def data_offset_past_header(self) -> int:
        """Number of bytes after the fixed header to skip to reach file data."""
        return self.filename_length + self.extra_length


class _ReadRequired(BadZipFile):
    """A bounded range needed to finish parsing the end records."""

    def __init__(self, offset: int, length: int, message: str) -> None:
        super().__init__(message)
        self.offset = offset
        self.length = length


def _end_record(
    tail: bytes,
    file_size: int,
    records: Mapping[int, bytes],
    offset: int,
    length: int,
) -> bytes:
    if offset < 0 or offset + length > file_size:
        raise BadZipFile("ZIP64 end record is outside the archive")
    tail_start = file_size - len(tail)
    if offset >= tail_start:
        return tail[offset - tail_start : offset - tail_start + length]
    if offset not in records:
        raise _ReadRequired(offset, length, "ZIP64 EOCD record is outside the fetched tail data")
    data = records[offset]
    if len(data) != length:
        raise BadZipFile("ZIP64 end record is truncated")
    return data


def _find_eocd_offset(tail: bytes) -> int:
    """Use zipfile's no-comment fast path and bounded signature search."""
    if (
        len(tail) >= EOCD_SIZE
        and tail[-EOCD_SIZE : -EOCD_SIZE + 4] == EOCD_SIGNATURE
        and tail[-2:] == b"\0\0"
    ):
        return len(tail) - EOCD_SIZE
    pos = tail.rfind(EOCD_SIGNATURE, max(0, len(tail) - EOCD_SIZE - 0xFFFF))
    if pos < 0:
        raise BadZipFile("Could not find End of Central Directory record")
    if len(tail) - pos < EOCD_SIZE:
        raise BadZipFile("EOCD record is truncated")
    return pos


def find_eocd(
    tail: bytes, file_size: int, *, records: Mapping[int, bytes] | None = None
) -> EOCDInfo:
    """Parse end records, requesting any missing ZIP64 header with _ReadRequired.

    The returned directory and entry offsets include any prepended data.
    Additional ranges are small, fixed-size headers; the ZIP64 extensible
    data sector does not need to be downloaded.
    """
    records = records or {}
    pos = _find_eocd_offset(tail)
    fields = EOCD_STRUCT.unpack_from(tail, pos)
    comment = tail[pos + EOCD_SIZE : pos + EOCD_SIZE + fields[7]]
    cd_entry_count, cd_size, cd_offset = fields[4:7]
    directory_end = file_size - len(tail) + pos
    locator_offset = directory_end - ZIP64_EOCD_LOCATOR_SIZE
    locator = b""
    if locator_offset >= 0:
        locator = _end_record(tail, file_size, records, locator_offset, ZIP64_EOCD_LOCATOR_SIZE)

    if locator.startswith(ZIP64_EOCD_LOCATOR_SIGNATURE):
        _, disk, relative_record_offset, disks = ZIP64_EOCD_LOCATOR_STRUCT.unpack(locator)
        if disk or disks > 1:
            raise BadZipFile("ZIP archives spanning multiple disks are not supported")
        record_offset = relative_record_offset
        if record_offset + ZIP64_EOCD_SIZE > locator_offset:
            raise BadZipFile("Invalid ZIP64 EOCD locator offset")
        record = _end_record(tail, file_size, records, record_offset, ZIP64_EOCD_SIZE)
        if not record.startswith(ZIP64_EOCD_SIGNATURE):
            # Self-extracting ZIPs can store offsets relative to the ZIP,
            # rather than the containing file. Match zipfile's fallback
            # for a fixed-size ZIP64 record immediately before the locator.
            record_offset = locator_offset - ZIP64_EOCD_SIZE
            record = _end_record(tail, file_size, records, record_offset, ZIP64_EOCD_SIZE)
        if not record.startswith(ZIP64_EOCD_SIGNATURE):
            raise BadZipFile("ZIP64 EOCD record signature mismatch")
        z64 = ZIP64_EOCD_STRUCT.unpack(record)
        if z64[1] < 44 or record_offset + 12 + z64[1] != locator_offset:
            raise BadZipFile("Invalid ZIP64 EOCD record size")
        cd_entry_count, cd_size, cd_offset = z64[7:10]
        if cd_offset + cd_size != relative_record_offset:
            raise BadZipFile("Invalid ZIP64 central directory offset or size")
        directory_end = record_offset
    # A count of exactly 65535 does not require ZIP64. The locator's
    # signature, not the count alone, determines whether ZIP64 is present.
    archive_offset = directory_end - cd_size - cd_offset
    if cd_offset + archive_offset < 0:
        raise BadZipFile("Invalid central directory offset or size")
    return EOCDInfo(
        cd_offset=cd_offset + archive_offset,
        cd_size=cd_size,
        cd_entry_count=cd_entry_count,
        archive_offset=archive_offset,
        comment=comment,
    )


def parse_central_directory(data: bytes) -> Iterator[CentralDirEntry]:
    """Yield central directory entries from raw bytes.

    Args:
        data: The raw central directory data.

    Yields:
        CentralDirEntry objects in directory order. As in zipfile,
        directory size determines the entries rather than the end record's count.

    Raises:
        BadZipFile: If the data is malformed.
    """
    offset = 0

    while offset < len(data):
        if offset + CENTRAL_DIR_SIZE > len(data):
            raise BadZipFile("Central directory is truncated")

        if data[offset : offset + 4] != CENTRAL_DIR_SIGNATURE:
            raise BadZipFile(f"Expected central directory signature at offset {offset}")

        fields = CENTRAL_DIR_STRUCT.unpack_from(data, offset)
        # fields: signature(0), version_made(1), version_needed(2), flags(3),
        #         compression(4), mod_time(5), mod_date(6), crc32(7),
        #         compressed_size(8), uncompressed_size(9), filename_length(10),
        #         extra_length(11), comment_length(12), disk_start(13),
        #         internal_attr(14), external_attr(15), header_offset(16)

        filename_length = fields[10]
        extra_length = fields[11]
        comment_length = fields[12]

        var_start = offset + CENTRAL_DIR_SIZE
        var_end = var_start + filename_length + extra_length + comment_length

        filename = data[var_start : var_start + filename_length]
        extra = data[var_start + filename_length : var_start + filename_length + extra_length]
        comment = data[
            var_start + filename_length + extra_length : var_start
            + filename_length
            + extra_length
            + comment_length
        ]

        yield CentralDirEntry(
            version_made_by=fields[1],
            version_needed=fields[2],
            flags=fields[3],
            compression_method=fields[4],
            mod_time=fields[5],
            mod_date=fields[6],
            crc32=fields[7],
            compressed_size=fields[8],
            uncompressed_size=fields[9],
            disk_start=fields[13],
            internal_attr=fields[14],
            external_attr=fields[15],
            header_offset=fields[16],
            filename=filename,
            extra=extra,
            comment=comment,
        )
        offset = var_end


def parse_local_file_header(data: bytes, *, offset: int = 0) -> LocalHeaderInfo:
    """Parse a local file header to determine the data offset.

    Args:
        data: At least LOCAL_FILE_HEADER_SIZE bytes of the local header.
        offset: Position of the header within data.

    Returns:
        LocalHeaderInfo with filename and extra field lengths.

    Raises:
        BadZipFile: If the header is invalid.
    """
    if offset < 0 or len(data) - offset < LOCAL_FILE_HEADER_SIZE:
        raise BadZipFile("Local file header is truncated")

    fields = LOCAL_FILE_HEADER_STRUCT.unpack_from(data, offset)
    # fields: signature(0), version_needed(1), flags(2), compression(3),
    #         mod_time(4), mod_date(5), crc32(6), compressed_size(7),
    #         uncompressed_size(8), filename_length(9), extra_length(10)

    if fields[0] != LOCAL_FILE_HEADER_SIGNATURE:
        raise BadZipFile("Invalid local file header signature")

    return LocalHeaderInfo(
        filename_length=fields[9],
        extra_length=fields[10],
        flags=fields[2],
    )
