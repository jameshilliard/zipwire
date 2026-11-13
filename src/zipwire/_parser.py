"""EOCD search and central directory walking.

All functions operate on `bytes` - no IO. Unpacking, signature
checks, and validation of the individual records live in
`zipwire._zipstructs`; this module orchestrates them. It is shared by
both the sync and async code paths.
"""

from __future__ import annotations

from dataclasses import dataclass

from zipwire._errors import BadZipFile
from zipwire._zipstructs import (
    CentralDirHeaderRecord,
    EOCDRecord,
    Zip64EocdLocatorRecord,
    Zip64EocdRecord,
)


@dataclass(frozen=True, slots=True)
class EOCDInfo:
    """Parsed End of Central Directory information.

    The location and entry count come from the authoritative end
    record - the ZIP64 EOCD record when the classic record holds
    sentinel values.  *comment* is the .ZIP file comment, which only
    the classic record carries.
    """

    cd_offset: int
    cd_size: int
    cd_entry_count: int
    comment: bytes = b""


def find_eocd(tail: bytes, file_size: int) -> EOCDInfo:
    """Find and parse the End of Central Directory record.

    Args:
        tail: The tail of the file (up to 65557 bytes).
        file_size: Total size of the ZIP file.

    Returns:
        EOCDInfo with the central directory location and the .ZIP
        file comment.

    Raises:
        BadZipFile: If no valid EOCD record is found.
    """
    pos = tail.rfind(EOCDRecord.SIGNATURE)
    if pos == -1:
        raise BadZipFile("Could not find End of Central Directory record")

    eocd = EOCDRecord.parse(tail, pos)

    if eocd.has_zip64_sentinels:
        return _parse_zip64_eocd(tail, eocd, pos, file_size)

    return EOCDInfo(
        cd_offset=eocd.cd_offset,
        cd_size=eocd.cd_size,
        cd_entry_count=eocd.entries_total,
        comment=eocd.comment,
    )


def _parse_zip64_eocd(
    tail: bytes,
    eocd: EOCDRecord,
    eocd_pos: int,
    file_size: int,
) -> EOCDInfo:
    """Parse ZIP64 EOCD locator and record.

    Args:
        tail: The tail bytes of the file.
        eocd: The parsed classic EOCD record.
        eocd_pos: Position of the regular EOCD in `tail`.
        file_size: Total file size.

    Returns:
        EOCDInfo with 64-bit values and the classic record's comment.

    Raises:
        BadZipFile: If ZIP64 structures are missing or invalid.
    """
    # ZIP64 EOCD Locator is right before the regular EOCD
    locator_pos = eocd_pos - Zip64EocdLocatorRecord.FIXED_SIZE
    if locator_pos < 0:
        raise BadZipFile("ZIP64 EOCD locator not found (not enough data before EOCD)")

    # parse() checks the locator signature and rejects spanning archives.
    locator = Zip64EocdLocatorRecord.parse(tail, locator_pos)
    zip64_eocd_abs_offset = locator.zip64_eocd_offset

    # The record offset is absolute in the file.
    tail_start = file_size - len(tail)
    locator_abs_offset = tail_start + locator_pos

    if zip64_eocd_abs_offset + Zip64EocdRecord.FIXED_SIZE > locator_abs_offset:
        raise BadZipFile("Corrupt zip64 end of central directory locator")

    zip64_eocd_rel = zip64_eocd_abs_offset - tail_start

    if zip64_eocd_rel < 0 or zip64_eocd_rel + Zip64EocdRecord.FIXED_SIZE > len(tail):
        raise BadZipFile("ZIP64 EOCD record is outside the fetched tail data")

    # parse() checks the record signature and that the record size covers
    # the fixed part.  The record's own disk fields and per-disk entry
    # count are ignored, as in zipfile's _EndRecData64.  Multidisk
    # archives are rejected from the classic EOCD and the locator,
    # which is where zipfile checks for them too.
    record = Zip64EocdRecord.parse(tail, zip64_eocd_rel)

    # Like zipfile, require the central directory, the ZIP64 EOCD record,
    # and the locator to be adjacent.
    if record.cd_offset + record.cd_size != zip64_eocd_abs_offset:
        raise BadZipFile("Corrupt zip64 end of central directory record")
    if zip64_eocd_abs_offset + 12 + record.record_size != locator_abs_offset:
        raise BadZipFile("Corrupt zip64 end of central directory record")

    return EOCDInfo(
        cd_offset=record.cd_offset,
        cd_size=record.cd_size,
        cd_entry_count=record.entries_total,
        comment=eocd.comment,
    )


def parse_central_directory(data: bytes, entry_count: int) -> list[CentralDirHeaderRecord]:
    """Parse all central directory entries from raw bytes.

    Args:
        data: The raw central directory data.
        entry_count: Expected number of entries.

    Returns:
        List of CentralDirHeaderRecord objects.

    Raises:
        BadZipFile: If the data is malformed.
    """
    entries: list[CentralDirHeaderRecord] = []
    offset = 0

    for _ in range(entry_count):
        # parse() rejects a truncated fixed part, a wrong signature,
        # and variable parts that extend past the end of the data.
        record = CentralDirHeaderRecord.parse(data, offset)
        entries.append(record)
        offset += record.total_size

    return entries
