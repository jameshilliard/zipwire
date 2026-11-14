"""ZipInfo subclass that is fully compatible with stdlib zipfile.ZipInfo."""

from __future__ import annotations

import os
import zipfile

from zipwire._constants import ExtraDataId
from zipwire._zipstructs import (
    CentralDirHeaderRecord,
    UnicodePathExtraField,
    Zip64ExtendedInfo,
    find_extra_field,
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
        info.extract_version = entry.version_needed
        info.flag_bits = entry.flags
        info.volume = entry.disk_start
        info.extra = entry.extra
        info.comment = entry.comment

        field = find_extra_field(entry.extra, ExtraDataId.ZIP64)
        if field is not None:
            zip64 = Zip64ExtendedInfo.resolve(field.data, entry)
            if zip64.file_size is not None:
                info.file_size = zip64.file_size
            if zip64.compress_size is not None:
                info.compress_size = zip64.compress_size
            if zip64.header_offset is not None:
                info.header_offset = zip64.header_offset
            if zip64.disk_start is not None:
                info.volume = zip64.disk_start

        # A matching Unicode Path field carries the UTF-8 form of a
        # legacy-encoded name and replaces it; ``orig_filename`` keeps
        # the central directory form for the local header name check.
        field = find_extra_field(entry.extra, ExtraDataId.UNICODE_PATH)
        if field is not None:
            unicode_path = UnicodePathExtraField.resolve(field.data, entry.filename)
            if unicode_path is not None:
                name = unicode_path.name
                if os.sep != "/" and os.sep in name:
                    name = name.replace(os.sep, "/")
                info.filename = name

        return info
