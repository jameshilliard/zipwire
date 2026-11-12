"""Pure ZIP parsing: EOCD, central directory, and local file header.

All functions operate on `bytes` - no IO. This module is shared by both
the sync and async code paths.
"""

from __future__ import annotations

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


@dataclass(frozen=True, slots=True)
class EOCDInfo:
    """Parsed End of Central Directory information."""

    cd_offset: int
    cd_size: int
    cd_entry_count: int


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
    """Parsed local file header - just enough to find the data offset.

    *filename* holds the raw filename bytes when *data* contained all
    of them and ``None`` when it ended inside the filename.
    """

    filename_length: int
    extra_length: int
    filename: bytes | None

    @property
    def data_offset_past_header(self) -> int:
        """Number of bytes after the fixed header to skip to reach file data."""
        return self.filename_length + self.extra_length


def find_eocd(tail: bytes, file_size: int) -> EOCDInfo:
    """Find and parse the End of Central Directory record.

    Args:
        tail: The tail of the file (up to 65557 bytes).
        file_size: Total size of the ZIP file.

    Returns:
        EOCDInfo with central directory location.

    Raises:
        BadZipFile: If no valid EOCD record is found.
    """
    # Search backwards for EOCD signature
    pos = tail.rfind(EOCD_SIGNATURE)
    if pos == -1:
        raise BadZipFile("Could not find End of Central Directory record")

    if len(tail) - pos < EOCD_SIZE:
        raise BadZipFile("EOCD record is truncated")

    fields = EOCD_STRUCT.unpack_from(tail, pos)
    # fields: signature, disk_num, disk_cd_start, cd_entries_this_disk,
    #         cd_entries_total, cd_size, cd_offset, comment_length
    disk_num = fields[1]
    disk_cd_start = fields[2]
    if disk_num != 0 or disk_cd_start != 0:
        raise BadZipFile("zipfiles that span multiple disks are not supported")

    cd_entry_count = fields[4]
    cd_size = fields[5]
    cd_offset = fields[6]

    # Check for ZIP64 - indicated by 0xFFFF or 0xFFFFFFFF sentinel values
    is_zip64 = cd_entry_count == 0xFFFF or cd_size == 0xFFFFFFFF or cd_offset == 0xFFFFFFFF

    if is_zip64:
        return _parse_zip64_eocd(tail, pos, file_size)

    return EOCDInfo(
        cd_offset=cd_offset,
        cd_size=cd_size,
        cd_entry_count=cd_entry_count,
    )


def _parse_zip64_eocd(tail: bytes, eocd_pos: int, file_size: int) -> EOCDInfo:
    """Parse ZIP64 EOCD locator and record.

    Args:
        tail: The tail bytes of the file.
        eocd_pos: Position of the regular EOCD in `tail`.
        file_size: Total file size.

    Returns:
        EOCDInfo with 64-bit values.

    Raises:
        BadZipFile: If ZIP64 structures are missing or invalid.
    """
    # ZIP64 EOCD Locator is right before the regular EOCD
    locator_pos = eocd_pos - ZIP64_EOCD_LOCATOR_SIZE
    if locator_pos < 0:
        raise BadZipFile("ZIP64 EOCD locator not found (not enough data before EOCD)")

    locator_sig = tail[locator_pos : locator_pos + 4]
    if locator_sig != ZIP64_EOCD_LOCATOR_SIGNATURE:
        raise BadZipFile("ZIP64 EOCD locator signature mismatch")

    locator_fields = ZIP64_EOCD_LOCATOR_STRUCT.unpack_from(tail, locator_pos)
    # fields: signature, disk_with_zip64_eocd, zip64_eocd_offset, total_disks
    disk_with_zip64_eocd = locator_fields[1]
    zip64_eocd_abs_offset = locator_fields[2]
    total_disks = locator_fields[3]
    if disk_with_zip64_eocd != 0 or total_disks > 1:
        raise BadZipFile("zipfiles that span multiple disks are not supported")

    # The ZIP64 EOCD record offset is absolute in the file. Convert to tail-relative.
    tail_start = file_size - len(tail)
    locator_abs_offset = tail_start + locator_pos

    # The record must start before the locator and fit between the central
    # directory and the locator.
    if zip64_eocd_abs_offset + ZIP64_EOCD_SIZE > locator_abs_offset:
        raise BadZipFile("Corrupt zip64 end of central directory locator")

    zip64_eocd_rel = zip64_eocd_abs_offset - tail_start

    if zip64_eocd_rel < 0 or zip64_eocd_rel + ZIP64_EOCD_SIZE > len(tail):
        raise BadZipFile("ZIP64 EOCD record is outside the fetched tail data")

    z64_sig = tail[zip64_eocd_rel : zip64_eocd_rel + 4]
    if z64_sig != ZIP64_EOCD_SIGNATURE:
        raise BadZipFile("ZIP64 EOCD record signature mismatch")

    z64_fields = ZIP64_EOCD_STRUCT.unpack_from(tail, zip64_eocd_rel)
    # fields: signature, size_of_record, version_made, version_needed,
    #         disk_num, disk_cd_start, cd_entries_this_disk,
    #         cd_entries_total, cd_size, cd_offset
    # The record's own disk fields and per-disk entry count are ignored,
    # as in zipfile's _EndRecData64.  Multidisk archives are rejected
    # from the classic EOCD and the locator, which is where zipfile
    # checks for them too.
    record_size = z64_fields[1]
    cd_entry_count = z64_fields[7]
    cd_size = z64_fields[8]
    cd_offset = z64_fields[9]

    # Like zipfile, require the central directory to end exactly where the
    # ZIP64 EOCD record begins, and the record (with any extensible data)
    # to end exactly where the locator begins.
    if cd_offset + cd_size != zip64_eocd_abs_offset:
        raise BadZipFile("Corrupt zip64 end of central directory record")
    if zip64_eocd_abs_offset + 12 + record_size != locator_abs_offset:
        raise BadZipFile("Corrupt zip64 end of central directory record")

    return EOCDInfo(
        cd_offset=cd_offset,
        cd_size=cd_size,
        cd_entry_count=cd_entry_count,
    )


def parse_central_directory(data: bytes, entry_count: int) -> list[CentralDirEntry]:
    """Parse all central directory entries from raw bytes.

    Args:
        data: The raw central directory data.
        entry_count: Expected number of entries.

    Returns:
        List of CentralDirEntry objects.

    Raises:
        BadZipFile: If the data is malformed.
    """
    entries: list[CentralDirEntry] = []
    offset = 0

    for _ in range(entry_count):
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

        if var_end > len(data):
            raise BadZipFile("Central directory entry extends past end of data")

        filename = data[var_start : var_start + filename_length]
        extra = data[var_start + filename_length : var_start + filename_length + extra_length]
        comment = data[
            var_start + filename_length + extra_length : var_start
            + filename_length
            + extra_length
            + comment_length
        ]

        entry = CentralDirEntry(
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
        entries.append(entry)
        offset = var_end

    return entries


def parse_local_file_header(data: bytes) -> LocalHeaderInfo:
    """Parse a local file header to determine the data offset.

    Args:
        data: At least LOCAL_FILE_HEADER_SIZE bytes of the local
            header.  The raw filename is extracted when *data* also
            contains all ``filename_length`` bytes of it.

    Returns:
        LocalHeaderInfo with the filename and extra field lengths and
        the raw filename bytes when they are contained in *data*.

    Raises:
        BadZipFile: If the header is invalid.
    """
    if len(data) < LOCAL_FILE_HEADER_SIZE:
        raise BadZipFile("Local file header is truncated")

    fields = LOCAL_FILE_HEADER_STRUCT.unpack_from(data, 0)
    # fields: signature(0), version_needed(1), flags(2), compression(3),
    #         mod_time(4), mod_date(5), crc32(6), compressed_size(7),
    #         uncompressed_size(8), filename_length(9), extra_length(10)

    if fields[0] != LOCAL_FILE_HEADER_SIGNATURE:
        raise BadZipFile("Invalid local file header signature")

    filename_length = fields[9]
    filename_end = LOCAL_FILE_HEADER_SIZE + filename_length
    if filename_end <= len(data):
        filename: bytes | None = data[LOCAL_FILE_HEADER_SIZE:filename_end]
    else:
        filename = None

    return LocalHeaderInfo(
        filename_length=filename_length,
        extra_length=fields[10],
        filename=filename,
    )
