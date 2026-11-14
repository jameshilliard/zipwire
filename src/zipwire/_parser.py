"""EOCD search and central directory walking using declarative ZIP records."""

from __future__ import annotations

import typing
from dataclasses import dataclass

from zipwire._errors import BadZipFile
from zipwire._zipstructs import (
    CentralDirHeaderRecord,
    EOCDRecord,
    Zip64EocdLocatorRecord,
    Zip64EocdRecord,
)

if typing.TYPE_CHECKING:
    from collections.abc import Iterator, Mapping


@dataclass(frozen=True, slots=True)
class EOCDInfo:
    """Directory location, archive comment, and adjustment for prepended data."""

    cd_offset: int
    cd_size: int
    cd_entry_count: int
    comment: bytes = b""
    archive_offset: int = 0


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
    size = EOCDRecord.FIXED_SIZE
    if (
        len(tail) >= size
        and tail[-size : -size + 4] == EOCDRecord.SIGNATURE
        and tail[-2:] == b"\0\0"
    ):
        return len(tail) - size
    pos = tail.rfind(EOCDRecord.SIGNATURE, max(0, len(tail) - EOCDRecord.MAX_SIZE))
    if pos < 0:
        raise BadZipFile("Could not find End of Central Directory record")
    return pos


def find_eocd(
    tail: bytes, file_size: int, *, records: Mapping[int, bytes] | None = None
) -> EOCDInfo:
    """Parse end records, requesting any missing ZIP64 header with _ReadRequired.

    Directory and entry offsets include any prepended data. Additional
    ranges are small, fixed-size headers; the ZIP64 extensible data sector
    does not need to be downloaded. Record validation rejects malformed
    end records and comments, including claims of multiple disks.
    """
    records = records or {}
    pos = _find_eocd_offset(tail)
    eocd = EOCDRecord.parse(tail, pos)
    cd_entry_count: int = eocd.entries_total
    cd_size: int = eocd.cd_size
    cd_offset: int = eocd.cd_offset
    directory_end = file_size - len(tail) + pos
    locator_offset = directory_end - Zip64EocdLocatorRecord.FIXED_SIZE
    locator_data = b""
    if locator_offset >= 0:
        locator_data = _end_record(
            tail, file_size, records, locator_offset, Zip64EocdLocatorRecord.FIXED_SIZE
        )
    if locator_data.startswith(Zip64EocdLocatorRecord.SIGNATURE):
        locator = Zip64EocdLocatorRecord.parse(locator_data)
        relative_record_offset = locator.zip64_eocd_offset
        record_offset: int = relative_record_offset
        if record_offset + Zip64EocdRecord.FIXED_SIZE > locator_offset:
            raise BadZipFile("Invalid ZIP64 EOCD locator offset")
        record_data = _end_record(
            tail, file_size, records, record_offset, Zip64EocdRecord.FIXED_SIZE
        )
        if not record_data.startswith(Zip64EocdRecord.SIGNATURE):
            # A prepended executable can leave ZIP-relative stored offsets.
            record_offset = locator_offset - Zip64EocdRecord.FIXED_SIZE
            record_data = _end_record(
                tail, file_size, records, record_offset, Zip64EocdRecord.FIXED_SIZE
            )
        record = Zip64EocdRecord.parse(record_data)
        if record_offset + 12 + record.record_size != locator_offset:
            raise BadZipFile("Invalid ZIP64 EOCD record size")
        cd_entry_count, cd_size, cd_offset = record.entries_total, record.cd_size, record.cd_offset
        if cd_offset + cd_size != relative_record_offset:
            raise BadZipFile("Invalid ZIP64 central directory offset or size")
        directory_end = record_offset
    # A count of exactly 65535 does not require ZIP64: inspect the locator.
    archive_offset = directory_end - cd_size - cd_offset
    if cd_offset + archive_offset < 0:
        raise BadZipFile("Invalid central directory offset or size")
    return EOCDInfo(
        cd_offset=cd_offset + archive_offset,
        cd_size=cd_size,
        cd_entry_count=cd_entry_count,
        comment=eocd.comment,
        archive_offset=archive_offset,
    )


def parse_central_directory(data: bytes) -> Iterator[CentralDirHeaderRecord]:
    """Yield validated directory records, using directory size as the boundary."""
    offset = 0
    while offset < len(data):
        record = CentralDirHeaderRecord.parse(data, offset)
        yield record
        offset += record.total_size
