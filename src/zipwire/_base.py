"""Common base class for sync and async remote ZIP readers."""

from __future__ import annotations

import os
import sys
import warnings

from zipwire._constants import HeaderFlag
from zipwire._errors import (
    BadZipFile,
    FileNotFoundInZip,
    UnsupportedFeature,
)
from zipwire._parser import EOCDInfo, parse_central_directory
from zipwire._zipinfo import RemoteZipInfo
from zipwire._zipstructs import (
    EOCDRecord,
    LocalFileHeaderRecord,
    filename_encoding,
)

_NOT_LOADED = "archive not loaded; use as a context manager"


class RemoteZipBase:
    """Shared state and sync accessors for remote ZIP readers.

    This class holds the parsed central directory entries, the name
    index, file size, and EOCD record.  It is not meant to be
    instantiated directly.
    """

    def __init__(self) -> None:
        self._entries: list[RemoteZipInfo] | None = None
        self._name_index: dict[str, RemoteZipInfo] | None = None
        self._file_size: int | None = None
        self._eocd: EOCDInfo | None = None

    def _clear(self) -> None:
        """Reset all loaded state."""
        self._entries = None
        self._name_index = None
        self._file_size = None
        self._eocd = None

    def _validate_cd_bounds(self, file_size: int, eocd: EOCDInfo) -> None:
        """Check that the central directory lies inside the archive.

        This runs before the central directory is fetched, so a hostile
        end record cannot make zipwire request an arbitrary range.

        Raises:
            BadZipFile: If the central directory extends beyond the end
                of the archive.
        """
        if eocd.cd_offset + eocd.cd_size > file_size:
            raise BadZipFile(
                f"Central directory ({eocd.cd_offset} + {eocd.cd_size} bytes) "
                f"extends beyond the end of the archive ({file_size} bytes)"
            )

    def _set_entries(
        self,
        cd_data: bytes,
        file_size: int,
        eocd: EOCDInfo,
    ) -> None:
        """Parse central directory data and store entries.

        Raises:
            BadZipFile: If the central directory lies beyond the end of
                the archive or the fetch returned the wrong number of
                bytes.
        """
        self._validate_cd_bounds(file_size, eocd)
        if len(cd_data) != eocd.cd_size:
            raise BadZipFile(
                f"Central directory fetch returned {len(cd_data)} of {eocd.cd_size} bytes"
            )
        raw_entries = parse_central_directory(cd_data, eocd.cd_entry_count)
        entries = [RemoteZipInfo._from_central_dir_entry(e) for e in raw_entries]
        # Record where each entry's data region ends: the start of the
        # next entry's local header, or the central directory for the
        # entry closest to it.  Like zipfile, this is used to detect
        # overlapped entries when reading.
        end_offset = eocd.cd_offset
        for info in sorted(entries, key=lambda i: i.header_offset, reverse=True):
            info._end_offset = end_offset
            end_offset = info.header_offset
        self._entries = entries
        self._name_index = {info.filename: info for info in entries}
        self._file_size = file_size
        self._eocd = eocd

    def _validate_entry(self, info: RemoteZipInfo) -> None:
        """Reject entries that zipwire cannot read.

        Checks the general purpose flags for features zipwire does not
        support and that the local file header lies within the archive.

        Raises:
            UnsupportedFeature: If the entry is encrypted (flag bit 0),
                uses strong encryption (flag bit 6), or compressed
                patched data (flag bit 5).
            BadZipFile: If the local file header offset lies beyond the
                end of the archive.
        """
        # Most specific reason first when several flags are set.
        if info.flag_bits & HeaderFlag.STRONG_ENCRYPTION:
            raise UnsupportedFeature(
                f"{info.filename!r}: strong encryption (general purpose flag bit 6)"
            )
        if info.flag_bits & HeaderFlag.COMPRESSED_PATCHED_DATA:
            raise UnsupportedFeature(
                f"{info.filename!r}: compressed patched data (general purpose flag bit 5)"
            )
        if info.flag_bits & HeaderFlag.ENCRYPTED:
            raise UnsupportedFeature(f"{info.filename!r}: encrypted")
        if info.header_offset + LocalFileHeaderRecord.FIXED_SIZE > self.file_size:
            raise BadZipFile(
                f"{info.filename!r}: local file header at offset {info.header_offset} "
                "is beyond the end of the archive"
            )

    def _validate_data_region(self, info: RemoteZipInfo, data_offset: int) -> None:
        """Check that an entry's compressed data stays inside its region.

        The data must end within the archive and before the next entry's
        local header (or the central directory), so a hostile central
        directory cannot make zipwire fetch another entry's data or the
        directory itself as file data.

        Raises:
            BadZipFile: If the data extends beyond the archive or
                overlaps the next entry (possible zip bomb).
        """
        data_end = data_offset + info.compress_size
        if data_end > self.file_size:
            raise BadZipFile(
                f"{info.filename!r}: compressed data extends beyond the end of the archive"
            )
        end_offset = info._end_offset
        if end_offset is not None and data_end > end_offset:
            if end_offset == info.header_offset:
                # Two entries share the same local header; zipfile warns
                # instead of raising for this case.
                message = f"Overlapped entries: {info.filename!r} (possible zip bomb)"
                if sys.version_info >= (3, 12):
                    # Attribute the warning to the caller regardless of
                    # how many frames zipwire adds below it.
                    warnings.warn(message, skip_file_prefixes=(os.path.dirname(__file__),))
                else:
                    # Points at the caller of read()/read_into(); the
                    # wheel tail path adds one frame.
                    warnings.warn(message, stacklevel=4)
            else:
                raise BadZipFile(f"Overlapped entries: {info.filename!r} (possible zip bomb)")

    def _check_local_filename(self, info: RemoteZipInfo, local_filename: bytes | None) -> None:
        """Compare the local file header name with the central directory name.

        Like ``zipfile.ZipFile.open()``, the raw filename bytes from the
        local header are compared against the central directory name
        encoded with the central directory's encoding, which the UTF-8
        flag selects.  A mismatch means the archive was crafted so that
        tools reading the local header see a different member name than
        the central directory reports.

        Raises:
            BadZipFile: If the names differ or the local filename bytes
                are missing.
        """
        if local_filename is None:
            raise BadZipFile(f"{info.filename!r}: local file header filename is truncated")
        expected = info.orig_filename.encode(filename_encoding(info.flag_bits))
        if local_filename != expected:
            raise BadZipFile(
                f"File name in directory {info.orig_filename!r} and "
                f"header {local_filename!r} differ."
            )

    @property
    def file_size(self) -> int:
        """Total size of the remote archive in bytes."""
        if self._file_size is None:
            raise RuntimeError(_NOT_LOADED)
        return self._file_size

    @property
    def eocd_info(self) -> EOCDInfo:
        """Parsed End of Central Directory information.

        Contains ``cd_offset``, ``cd_size``, ``cd_entry_count``, and
        ``comment`` (the .ZIP file comment).  The location and entry
        count come from the ZIP64 end record when the archive uses
        ZIP64 end records.
        """
        if self._eocd is None:
            raise RuntimeError(_NOT_LOADED)
        return self._eocd

    def _tail_read_size(self, file_size: int) -> int:
        """Return the number of bytes to fetch from the end of the file.

        Called during loading to determine the tail size.
        The default returns the maximum EOCD record size (fixed part
        plus the largest possible .ZIP file comment).  Subclasses may
        override this to request a larger tail.
        """
        return EOCDRecord.MAX_SIZE

    def _store_tail(self, tail_start: int, tail: bytes) -> None:
        """Retain the initial tail fetch.

        Called at the end of a successful load.  The default discards
        the tail; subclasses may override this to serve entries from
        the buffer.
        """

    def infolist(self) -> list[RemoteZipInfo]:
        """Return a list of RemoteZipInfo objects for all files in the archive."""
        if self._entries is None:
            raise RuntimeError(_NOT_LOADED)
        return list(self._entries)

    def namelist(self) -> list[str]:
        """Return a list of filenames in the archive."""
        if self._entries is None:
            raise RuntimeError(_NOT_LOADED)
        return [info.filename for info in self._entries]

    def getinfo(self, name: str) -> RemoteZipInfo:
        """Return the RemoteZipInfo for the given filename.

        Raises:
            FileNotFoundInZip: If the name is not in the archive.
        """
        if self._name_index is None:
            raise RuntimeError(_NOT_LOADED)
        try:
            return self._name_index[name]
        except KeyError:
            raise FileNotFoundInZip(name) from None
