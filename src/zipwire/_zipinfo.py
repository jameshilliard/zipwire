"""ZipInfo subclass that is fully compatible with stdlib zipfile.ZipInfo."""

from __future__ import annotations

import struct
import typing
import zipfile

from zipwire._constants import FLAG_UTF8_FILENAME, ZIP64_EXTRA_FIELD_ID
from zipwire._errors import BadZipFile

if typing.TYPE_CHECKING:
    from zipwire._parser import CentralDirEntry


class RemoteZipInfo(zipfile.ZipInfo):
    """A ZipInfo subclass for entries from a remote ZIP archive.

    ``isinstance(info, zipfile.ZipInfo)`` returns True. All standard
    attributes and methods (is_dir, FileHeader, etc.) are available.
    """

    # End of the entry's data region: the next entry's local header
    # offset, or the central directory offset for the last entry.
    # RemoteZip assigns it after parsing the central directory; it is
    # None for entries that have not been through that step.
    _end_offset: int | None = None

    @classmethod
    def _from_central_dir_entry(cls, entry: CentralDirEntry) -> RemoteZipInfo:
        """Construct a ZipInfo from a parsed central directory entry."""
        # Decode filename
        # Bit 11 of flags indicates UTF-8 encoding
        if entry.flags & FLAG_UTF8_FILENAME:
            filename = entry.filename.decode("utf-8")
        else:
            filename = entry.filename.decode("cp437")

        info = cls(filename)

        # Convert DOS date/time to tuple
        info.date_time = (
            ((entry.mod_date >> 9) & 0x7F) + 1980,  # year
            (entry.mod_date >> 5) & 0x0F,  # month
            entry.mod_date & 0x1F,  # day
            (entry.mod_time >> 11) & 0x1F,  # hour
            (entry.mod_time >> 5) & 0x3F,  # minute
            (entry.mod_time & 0x1F) * 2,  # second
        )

        info.compress_type = entry.compression_method
        info.CRC = entry.crc32
        info.compress_size = entry.compressed_size
        info.file_size = entry.uncompressed_size
        info.header_offset = entry.header_offset
        info.internal_attr = entry.internal_attr
        info.external_attr = entry.external_attr
        info.create_system = (entry.version_made_by >> 8) & 0xFF
        info.create_version = entry.version_made_by & 0xFF
        info.extract_version = entry.version_needed
        info.flag_bits = entry.flags
        info.volume = entry.disk_start
        info.extra = entry.extra
        info.comment = entry.comment

        # Handle ZIP64 extra field
        _apply_zip64_extra(info)

        return info


# Sentinel values that mark a field as stored in the ZIP64 extra field.
# The 64-bit sentinel can only appear on a round-tripped ZipInfo.
_ZIP64_SIZE_SENTINELS = (0xFFFFFFFF, 0xFFFFFFFFFFFFFFFF)


def _apply_zip64_extra(info: RemoteZipInfo) -> None:
    """Parse ZIP64 extra field and update sizes/offset if present.

    Raises:
        BadZipFile: If an extra field is truncated or a ZIP64 value that
            the entry's sentinel fields promise is missing.
    """
    extra = info.extra
    if not extra:
        return

    offset = 0
    while offset + 4 <= len(extra):
        header_id, data_size = struct.unpack_from("<HH", extra, offset)
        data_start = offset + 4
        if data_start + data_size > len(extra):
            raise BadZipFile(f"Corrupt extra field {header_id:#06x} (size={data_size})")

        if header_id == ZIP64_EXTRA_FIELD_ID:
            # ZIP64 extended information extra field
            # Fields appear in order only if the corresponding regular
            # field holds a sentinel value.
            data = extra[data_start : data_start + data_size]
            idx = 0
            if info.file_size in _ZIP64_SIZE_SENTINELS:
                info.file_size = _zip64_value(data, idx, "File size")
                idx += 8
            if info.compress_size == 0xFFFFFFFF:
                info.compress_size = _zip64_value(data, idx, "Compress size")
                idx += 8
            if info.header_offset == 0xFFFFFFFF:
                info.header_offset = _zip64_value(data, idx, "Header offset")
                idx += 8
            if info.volume == 0xFFFF:
                if idx + 4 > len(data):
                    raise BadZipFile("Corrupt zip64 extra field. Disk start number not found.")
                info.volume = struct.unpack_from("<I", data, idx)[0]
            return

        offset = data_start + data_size


def _zip64_value(data: bytes, idx: int, field: str) -> int:
    """Unpack one 8-byte ZIP64 value, raising BadZipFile if it is missing."""
    if idx + 8 > len(data):
        raise BadZipFile(f"Corrupt zip64 extra field. {field} not found.")
    return struct.unpack_from("<Q", data, idx)[0]
