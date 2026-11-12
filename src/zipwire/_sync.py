"""Synchronous remote ZIP reader."""

from __future__ import annotations

import contextlib
import typing

from zipwire._base import RemoteZipBase
from zipwire._constants import (
    LOCAL_FILE_HEADER_SIZE,
    PREFETCH_EXTRA,
    PREFETCH_THRESHOLD,
)
from zipwire._decompress import StreamingDecompressor, decompress
from zipwire._errors import BadZipFile, FileTooLarge, RangeRequestUnsupported
from zipwire._parser import find_eocd, parse_local_file_header
from zipwire._write import write_all
from zipwire._zipinfo import RemoteZipInfo

if typing.TYPE_CHECKING:
    from zipwire._types import SyncReader, Writable


class SyncRemoteZip(RemoteZipBase):
    """Read files from a remote ZIP archive using synchronous HTTP range requests.

    Must be used as a context manager.  The archive is loaded on entry
    and all internal state is cleared on exit.  Not thread-safe.

    Usage::

        with SyncRemoteZip(reader) as rz:
            for info in rz.infolist():
                print(info.filename)
            data = rz.read("path/to/file.txt")
    """

    def __init__(self, reader: SyncReader) -> None:
        super().__init__()
        self._reader = reader

    def __enter__(self) -> typing.Self:
        try:
            self._ensure_loaded()
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, *exc: object) -> None:
        self._clear()
        self.close()

    def close(self) -> None:
        """Close the underlying reader."""
        self._reader.close()

    @property
    def url(self) -> str:
        """The target URL of the underlying reader."""
        return self._reader.url

    def _ensure_loaded(self) -> None:
        """Fetch and parse the central directory.

        Stores the parsed entries via ``_set_entries()`` and the initial
        tail fetch via ``_store_tail()``.

        Raises:
            RuntimeError: If called more than once.
        """
        if self._entries is not None:
            raise RuntimeError("_ensure_loaded() must not be called more than once")

        # Step 1: HEAD to get total size, then fetch the tail for EOCD search.
        # We use HEAD + absolute range instead of a single suffix-range
        # request (bytes=-N) because some CDNs, notably Fastly which
        # fronts PyPI, do not support suffix byte ranges and return 501.
        head_headers = self._reader.head()
        cl = head_headers.get("content-length")
        if cl is None:
            raise RangeRequestUnsupported("Server did not return a Content-Length header")
        file_size = int(cl)
        fetch_size = min(file_size, self._tail_read_size(file_size))
        tail_start = max(0, file_size - fetch_size)
        tail, _ = self._reader.read_range(tail_start, file_size - tail_start)
        if len(tail) != file_size - tail_start:
            raise BadZipFile(
                f"Archive tail fetch returned {len(tail)} of {file_size - tail_start} bytes"
            )

        # Step 2: Parse EOCD
        eocd = find_eocd(tail, file_size)

        # Step 3: Fetch central directory and store parsed state
        self._validate_cd_bounds(file_size, eocd)
        cd_data, _ = self._reader.read_range(eocd.cd_offset, eocd.cd_size)
        self._set_entries(cd_data, file_size, eocd)
        self._store_tail(tail_start, tail)

    def _resolve_entry(
        self,
        name: str | RemoteZipInfo,
    ) -> tuple[RemoteZipInfo, int, bytes | None] | None:
        """Resolve *name* to a ZipInfo and compute the compressed data offset.

        Returns ``None`` for directory entries (no data to extract),
        or ``(info, data_offset, prefetched)`` for file entries.
        For small files (compressed size <= 50 KiB) the local header and
        compressed data are fetched in a single request and *prefetched*
        contains the compressed bytes.  Otherwise *prefetched* is ``None``.
        """
        info = name if isinstance(name, RemoteZipInfo) else self.getinfo(name)
        if info.is_dir():
            return None
        self._validate_entry(info)

        if info.compress_size <= PREFETCH_THRESHOLD:
            # Small file: fetch local header + compressed data in one request.
            # Clamp to the archive end: the size comes from the central
            # directory and must not turn into a request past the end.
            fetch_size = LOCAL_FILE_HEADER_SIZE + PREFETCH_EXTRA + info.compress_size
            fetch_size = min(fetch_size, self.file_size - info.header_offset)
            buf, _ = self._reader.read_range(info.header_offset, fetch_size)
            local_header = parse_local_file_header(buf)
            data_start = LOCAL_FILE_HEADER_SIZE + local_header.data_offset_past_header
            data_offset = info.header_offset + data_start
            if data_start + info.compress_size <= len(buf):
                self._validate_data_region(info, data_offset)
                return info, data_offset, buf[data_start : data_start + info.compress_size]
            # Rare: variable fields exceeded PREFETCH_EXTRA, fall through
        else:
            buf, _ = self._reader.read_range(info.header_offset, LOCAL_FILE_HEADER_SIZE)
            local_header = parse_local_file_header(buf)
            data_offset = (
                info.header_offset + LOCAL_FILE_HEADER_SIZE + local_header.data_offset_past_header
            )

        self._validate_data_region(info, data_offset)
        return info, data_offset, None

    def read(
        self,
        name: str | RemoteZipInfo,
        *,
        max_file_size: int | None = None,
    ) -> bytes:
        """Read and decompress a file from the archive.

        Args:
            name: Filename string or RemoteZipInfo object.
            max_file_size: Optional limit on uncompressed size in bytes.
                Raises :exc:`~zipwire.FileTooLarge` if the entry's
                ``file_size`` exceeds this limit.

        Returns:
            The decompressed file contents.

        .. warning::

            This method loads the entire decompressed file into memory.
            It checks the entry's declared ``file_size`` against
            *max_file_size*, but a crafted archive may lie about its
            uncompressed size.  Use :meth:`read_into` for defence in
            depth: it enforces the size limit during decompression.
            The decompressed size is verified against the declared
            ``file_size`` and raises :exc:`~zipwire.SizeMismatch` when
            they differ.
        """
        resolved = self._resolve_entry(name)
        if resolved is None:
            return b""
        info, data_offset, prefetched = resolved
        if max_file_size is not None and info.file_size > max_file_size:
            raise FileTooLarge(info.filename, info.file_size, max_file_size)
        if prefetched is None:
            prefetched, _ = self._reader.read_range(data_offset, info.compress_size)
            if len(prefetched) != info.compress_size:
                raise BadZipFile(
                    f"{info.filename!r}: data fetch returned {len(prefetched)} "
                    f"of {info.compress_size} bytes"
                )
        return decompress(
            prefetched,
            info.compress_type,
            info.file_size,
            info.CRC,
        )

    def read_into(
        self,
        name: str | RemoteZipInfo,
        dest: Writable,
        *,
        max_file_size: int | None = None,
    ) -> None:
        """Decompress a file from the archive into a writable destination.

        Unlike :meth:`read`, this truly streams: the compressed payload is
        fetched in chunks via :meth:`stream_range` and each chunk is
        decompressed and written immediately, keeping peak memory low.
        Decompressed output is handed to *dest* in chunks of at most
        2 MiB, even when the entry expands far beyond its compressed size.

        The streaming decompressor also enforces *max_file_size* during
        decompression, aborting if the actual output exceeds the limit
        regardless of what the entry metadata claims.  A compressed
        stream that ends prematurely raises
        :exc:`~zipwire.TruncatedStream`.  Decompressed output beyond the
        declared ``file_size`` aborts the transfer and raises
        :exc:`~zipwire.SizeMismatch`; a shortfall is detected once the
        stream ends.

        Writes are checked: a destination that accepts fewer bytes than
        given is retried with the remaining bytes, while a byte count that
        is not an :class:`int`, or is zero, negative, or oversized, raises
        :exc:`OSError`.  A ``write()`` that returns ``None`` is assumed to
        have accepted the whole chunk.

        The caller owns *dest*: this method does not flush or close it.
        Errors may leave partial output; only a successful return indicates
        that CRC-32 verification passed.

        The streamed response is closed when extraction finishes or fails.
        If closing the stream raises, the close error propagates and may
        mask the original exception, which remains available in its
        ``__context__`` chain.

        Args:
            name: Filename string or RemoteZipInfo object.
            dest: A writable file-like object (e.g. ``io.BytesIO``, open file).
            max_file_size: Optional limit on uncompressed size in bytes.
                Raises :exc:`~zipwire.FileTooLarge` if the entry's
                ``file_size`` exceeds this limit, or if the actual
                decompressed output exceeds it.
        """
        resolved = self._resolve_entry(name)
        if resolved is None:
            return
        info, data_offset, prefetched = resolved
        if max_file_size is not None and info.file_size > max_file_size:
            raise FileTooLarge(info.filename, info.file_size, max_file_size)
        sd = StreamingDecompressor(
            info.compress_type,
            info.CRC,
            filename=info.filename,
            max_output_size=max_file_size,
            expected_size=info.file_size,
        )
        if prefetched is not None:
            write_all(dest, sd.feed_chunks(prefetched))
        else:
            stream = self._reader.stream_range(data_offset, info.compress_size)
            with contextlib.closing(stream):
                for chunk in stream:
                    write_all(dest, sd.feed_chunks(chunk))
        sd.finish()
