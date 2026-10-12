"""Exception hierarchy for zipwire."""

from __future__ import annotations

import zipfile


class ZipwireError(Exception):
    """Base exception for all zipwire errors."""


class BadZipFile(ZipwireError, zipfile.BadZipFile):
    """The data does not appear to be a valid ZIP archive."""


class UnsupportedCompression(ZipwireError, NotImplementedError):
    """The file uses an unsupported compression method."""

    def __init__(self, method: int) -> None:
        self.method = method
        super().__init__(f"Unsupported compression method: {method}")


class CRCMismatch(BadZipFile):
    """CRC-32 check failed after decompression."""

    def __init__(self, expected: int, actual: int) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__(f"CRC-32 mismatch: expected {expected:#010x}, got {actual:#010x}")


class RangeRequestUnsupported(ZipwireError):
    """The HTTP server does not support range requests."""


class FileTooLarge(ZipwireError):
    """The entry's uncompressed size exceeds the configured limit."""

    def __init__(self, filename: str, file_size: int, max_size: int) -> None:
        self.filename = filename
        self.file_size = file_size
        self.max_size = max_size
        super().__init__(
            f"{filename!r}: uncompressed size {file_size} exceeds limit {max_size} "
            f"(set max_file_size to increase or None to disable)"
        )


class FileNotFoundInZip(ZipwireError, KeyError):
    """The requested file was not found in the ZIP archive."""

    def __init__(self, filename: str) -> None:
        self.filename = filename
        super().__init__(f"File not found in archive: {filename!r}")
