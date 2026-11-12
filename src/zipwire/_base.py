"""Common base class for sync and async remote ZIP readers."""

from __future__ import annotations

from zipwire._constants import MAX_EOCD_SEARCH
from zipwire._errors import FileNotFoundInZip
from zipwire._parser import EOCDInfo, parse_central_directory
from zipwire._zipinfo import RemoteZipInfo

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

    def _set_entries(
        self,
        cd_data: bytes,
        file_size: int,
        eocd: EOCDInfo,
    ) -> None:
        """Parse central directory data and store entries."""
        raw_entries = parse_central_directory(cd_data, eocd.cd_entry_count)
        entries = [RemoteZipInfo._from_central_dir_entry(e) for e in raw_entries]
        self._entries = entries
        self._name_index = {info.filename: info for info in entries}
        self._file_size = file_size
        self._eocd = eocd

    @property
    def file_size(self) -> int:
        """Total size of the remote archive in bytes."""
        if self._file_size is None:
            raise RuntimeError(_NOT_LOADED)
        return self._file_size

    @property
    def eocd_info(self) -> EOCDInfo:
        """Parsed End of Central Directory record.

        Contains ``cd_offset``, ``cd_size``, and ``cd_entry_count``.
        """
        if self._eocd is None:
            raise RuntimeError(_NOT_LOADED)
        return self._eocd

    def _tail_read_size(self, file_size: int) -> int:
        """Return the number of bytes to fetch from the end of the file.

        Called during loading to determine the tail size.
        The default returns :data:`MAX_EOCD_SEARCH`.  Subclasses may
        override this to request a larger tail.
        """
        return MAX_EOCD_SEARCH

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
