"""ZipInfo subclass that is fully compatible with stdlib zipfile.ZipInfo."""

from __future__ import annotations

import struct
import typing
import warnings
import zipfile

try:
    from zlib import crc32
except ImportError:  # pragma: no cover
    from binascii import crc32

from zipwire._constants import ZIP64_EXTRA_FIELD_ID
from zipwire._errors import BadZipFile

if typing.TYPE_CHECKING:
    from collections.abc import Iterator

    from zipwire._parser import CentralDirEntry


class RemoteZipInfo(zipfile.ZipInfo):
    """A ZipInfo subclass for entries from a remote ZIP archive.

    ``isinstance(info, zipfile.ZipInfo)`` returns True. All standard
    attributes and methods (is_dir, FileHeader, etc.) are available.
    """

    _raw_time: int
    _end_offset: int | None

    @classmethod
    def _from_central_dir_entry(
        cls, entry: CentralDirEntry, *, metadata_encoding: str | None = None
    ) -> RemoteZipInfo:
        """Construct a ZipInfo from a parsed central directory entry."""
        # Decode filename
        # Bit 11 of flags indicates UTF-8 encoding
        if entry.flags & 0x800:
            filename = entry.filename.decode("utf-8")
        else:
            filename = entry.filename.decode(metadata_encoding or "cp437")

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
        info.extract_version = entry.version_needed & 0xFF
        info.reserved = entry.version_needed >> 8
        if info.extract_version > 63:
            raise NotImplementedError(f"ZIP version {info.extract_version / 10:.1f}")
        info.flag_bits = entry.flags
        info.volume = entry.disk_start
        info.extra = entry.extra
        info.comment = entry.comment
        info._raw_time = entry.mod_time

        _apply_extra_fields(info, entry.filename)
        return info


def _extra_fields(extra: bytes) -> Iterator[tuple[int, bytes]]:
    """Iterate over bounded extra fields, ignoring a trailing partial tag."""
    offset = 0
    while offset + 4 <= len(extra):
        header_id, data_size = struct.unpack_from("<HH", extra, offset)
        offset += 4
        if offset + data_size > len(extra):
            raise BadZipFile(f"Corrupt extra field {header_id:#06x}")
        yield header_id, extra[offset : offset + data_size]
        offset += data_size


def _apply_extra_fields(info: RemoteZipInfo, filename: bytes) -> None:
    """Apply ZIP64 and Unicode Path metadata in extra-field order."""
    for header_id, data in _extra_fields(info.extra):
        if header_id == ZIP64_EXTRA_FIELD_ID:
            offset = 0
            for name, sentinels in (
                ("file_size", (0xFFFFFFFF, 0xFFFFFFFFFFFFFFFF)),
                ("compress_size", (0xFFFFFFFF,)),
                ("header_offset", (0xFFFFFFFF,)),
            ):
                if getattr(info, name) not in sentinels:
                    continue
                if offset + 8 > len(data):
                    raise BadZipFile(f"Corrupt ZIP64 extra field: missing {name}")
                setattr(info, name, int.from_bytes(data[offset : offset + 8], "little"))
                offset += 8
            # zipfile leaves volume unchanged, ignoring the ZIP64 disk number.
        elif header_id == 0x7075:
            if len(data) < 5:
                raise BadZipFile("Corrupt Unicode Path extra field")
            if data[0] != 1 or int.from_bytes(data[1:5], "little") != crc32(filename):
                continue
            try:
                unicode_name = data[5:].decode("utf-8")
            except UnicodeDecodeError as exc:
                raise BadZipFile("Invalid UTF-8 in Unicode Path extra field") from exc
            if unicode_name:
                info.filename = zipfile.ZipInfo(unicode_name).filename
            else:
                warnings.warn("Empty Unicode Path extra field", stacklevel=3)
