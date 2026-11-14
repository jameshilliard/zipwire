"""ZipInfo subclass that is fully compatible with stdlib zipfile.ZipInfo."""

from __future__ import annotations

import os
import zipfile
from dataclasses import replace

from zipwire._constants import ExtraDataId
from zipwire._zipstructs import (
    CentralDirHeaderRecord,
    UnicodePathExtraField,
    Zip64ExtendedInfo,
    iter_extra_fields,
    uint16_t,
    uint32_t,
)


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
    _raw_time: int

    @classmethod
    def _from_central_dir_entry(
        cls,
        entry: CentralDirHeaderRecord,
        *,
        metadata_encoding: str | None = None,
    ) -> RemoteZipInfo:
        """Construct a ZipInfo from a parsed central directory entry."""
        info = cls(entry.decode_filename(metadata_encoding=metadata_encoding))

        info.date_time = entry.date_time
        info.compress_type = entry.compression_method
        info.CRC = entry.crc32
        info.compress_size = entry.compressed_size
        info.file_size = entry.uncompressed_size
        info.header_offset = entry.header_offset
        info.internal_attr = entry.internal_attr
        info.external_attr = entry.external_attr
        info.create_system = entry.create_system
        info.create_version = entry.create_version
        info.extract_version = entry.version_needed & 0xFF
        info.reserved = entry.version_needed >> 8
        if info.extract_version > 63:
            raise NotImplementedError(f"ZIP version {info.extract_version / 10:.1f}")
        info.flag_bits = entry.flags
        info.volume = entry.disk_start
        info.extra = entry.extra
        info.comment = entry.comment
        info._raw_time = entry.mod_time

        # Resolve fields in directory order, retaining the current values
        # so repeated ZIP64 fields only consume still-unresolved sentinels.
        current = entry
        for field in iter_extra_fields(entry.extra):
            if field.header_id == ExtraDataId.ZIP64:
                zip64 = Zip64ExtendedInfo.resolve(field.data, current)
                if zip64.file_size is not None:
                    info.file_size = zip64.file_size
                if zip64.compress_size is not None:
                    info.compress_size = zip64.compress_size
                if zip64.header_offset is not None:
                    info.header_offset = zip64.header_offset
                if zip64.disk_start is not None:
                    info.volume = zip64.disk_start
                current = replace(
                    current,
                    uncompressed_size=uint32_t(info.file_size),
                    compressed_size=uint32_t(info.compress_size),
                    header_offset=uint32_t(info.header_offset),
                    disk_start=uint16_t(info.volume),
                )
            elif field.header_id == ExtraDataId.UNICODE_PATH:
                unicode_path = UnicodePathExtraField.resolve(field.data, entry.filename)
                if unicode_path is not None:
                    name = unicode_path.name
                    if os.sep != "/" and os.sep in name:
                        name = name.replace(os.sep, "/")
                    info.filename = name

        return info
