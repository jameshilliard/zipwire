"""Exception hierarchy for zipwire."""

from __future__ import annotations


class ZipwireError(Exception):
    """Base exception for all zipwire errors."""


class BadZipFile(ZipwireError):
    """The data does not appear to be a valid ZIP archive."""


class UnsupportedCompression(ZipwireError):
    """The file uses an unsupported compression method."""

    def __init__(self, method: int) -> None:
        self.method = method
        super().__init__(f"Unsupported compression method: {method}")


class UnsupportedFeature(ZipwireError):
    """The archive or an entry uses a ZIP feature zipwire does not support."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class CRCMismatch(ZipwireError):
    """CRC-32 check failed after decompression."""

    def __init__(self, expected: int, actual: int) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__(f"CRC-32 mismatch: expected {expected:#010x}, got {actual:#010x}")


class TruncatedStream(ZipwireError):
    """A compressed stream ended early or received data past its end."""


class SizeMismatch(ZipwireError):
    """Decompressed size does not match the entry's declared file size."""

    def __init__(self, expected: int, actual: int) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__(f"Size mismatch: expected {expected} bytes, got {actual}")


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
