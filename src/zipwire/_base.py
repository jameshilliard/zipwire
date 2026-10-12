"""Common base class for sync and async remote ZIP readers."""

from __future__ import annotations

import warnings
import zipfile

from zipwire._constants import LOCAL_FILE_HEADER_SIZE, MAX_EOCD_SEARCH
from zipwire._errors import BadZipFile, FileNotFoundInZip, FileTooLarge
from zipwire._parser import EOCDInfo, LocalHeaderInfo, parse_central_directory
from zipwire._zipinfo import RemoteZipInfo

_NOT_LOADED = "archive not loaded; use as a context manager"


class RemoteZipBase:
    """Shared state and sync accessors for remote ZIP readers.

    This class holds the parsed central directory entries, the name
    index, file size, and EOCD record.  It is not meant to be
    instantiated directly.
    """

    def __init__(self, *, metadata_encoding: str | None = None) -> None:
        self.metadata_encoding = metadata_encoding
        self.pwd: bytes | None = None
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
        entries = [
            RemoteZipInfo._from_central_dir_entry(e, metadata_encoding=self.metadata_encoding)
            for e in parse_central_directory(cd_data)
        ]
        for info in entries:
            info.header_offset += eocd.archive_offset
        end_offset = eocd.cd_offset
        for info in reversed(sorted(entries, key=lambda info: info.header_offset)):
            info._end_offset = end_offset
            end_offset = info.header_offset
        self._entries = entries
        self._name_index = {info.filename: info for info in entries}
        self._file_size = file_size
        self._eocd = eocd

    @property
    def comment(self) -> bytes:
        """The archive comment as bytes."""
        return self.eocd_info.comment

    def setpassword(self, pwd: bytes | None) -> None:
        """Set the default password for reading traditionally encrypted entries."""
        if pwd and not isinstance(pwd, bytes):
            raise TypeError(f"pwd: expected bytes, got {type(pwd).__name__}")
        self.pwd = pwd if pwd else None

    def _entry_info(
        self, name: str | zipfile.ZipInfo, *, max_file_size: int | None = None
    ) -> zipfile.ZipInfo:
        if self._entries is None:
            raise RuntimeError(_NOT_LOADED)
        info = name if isinstance(name, zipfile.ZipInfo) else self.getinfo(name)
        if info.flag_bits & 0x20:
            raise NotImplementedError("Compressed patched data is not supported")
        if info.flag_bits & 0x40:
            raise NotImplementedError("Strong ZIP encryption is not supported")
        if info.extract_version > 63:
            raise NotImplementedError(f"ZIP version {info.extract_version / 10:.1f}")
        if (
            info.header_offset < 0
            or info.header_offset + LOCAL_FILE_HEADER_SIZE > self.eocd_info.cd_offset
        ):
            raise BadZipFile("Local header is outside the file data")
        if max_file_size is not None and info.file_size > max_file_size:
            raise FileTooLarge(info.filename, info.file_size, max_file_size)
        return info

    def _validate_local_header(
        self, info: zipfile.ZipInfo, data: bytes, header: LocalHeaderInfo, *, offset: int = 0
    ) -> int:
        """Validate the member name and compressed extent before reading data."""
        filename_start = offset + LOCAL_FILE_HEADER_SIZE
        filename_end = filename_start + header.filename_length
        if len(data) < filename_end:
            raise BadZipFile("Local filename is truncated")
        filename = data[filename_start:filename_end].decode(
            "utf-8" if header.flags & 0x800 else self.metadata_encoding or "cp437"
        )
        if filename != info.orig_filename:
            raise BadZipFile("Local and central directory filenames differ")
        data_offset = info.header_offset + LOCAL_FILE_HEADER_SIZE + header.data_offset_past_header
        end: int | None = getattr(info, "_end_offset", None)
        if end is None:
            end = self.eocd_info.cd_offset
        if data_offset + info.compress_size > end:
            message = f"Overlapped entries: {info.orig_filename!r} (possible zip bomb)"
            if end == info.header_offset:
                warnings.warn(message, stacklevel=3)
            else:
                raise BadZipFile(message)
        return data_offset

    def _password(
        self, info: zipfile.ZipInfo, pwd: bytes | None
    ) -> tuple[bytes | None, int | None]:
        if not info.flag_bits & 1:
            return None, None
        pwd = pwd or self.pwd
        if not pwd:
            raise RuntimeError(f"File {info.filename!r} is encrypted; a password is required")
        if not isinstance(pwd, bytes):
            raise TypeError(f"pwd: expected bytes, got {type(pwd).__name__}")
        if info.compress_size < 12:
            raise BadZipFile("Truncated ZIP encryption header")
        if info.flag_bits & 8:
            dos_time = (
                (info.date_time[3] << 11) | (info.date_time[4] << 5) | (info.date_time[5] // 2)
            )
            raw_time: int = getattr(info, "_raw_time", dos_time)
            check = (raw_time >> 8) & 0xFF
        else:
            check = info.CRC >> 24
        return pwd, check

    @property
    def file_size(self) -> int:
        """Total size of the remote archive in bytes."""
        if self._file_size is None:
            raise RuntimeError(_NOT_LOADED)
        return self._file_size

    @property
    def eocd_info(self) -> EOCDInfo:
        """Parsed End of Central Directory record.

        Contains directory offsets/counts, the prepended data size, and comment.
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
