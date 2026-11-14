"""Synchronous remote ZIP reader."""

from __future__ import annotations

import typing
from contextlib import ExitStack
from dataclasses import replace

from zipwire._base import RemoteZipBase
from zipwire._constants import PREFETCH_EXTRA, PREFETCH_THRESHOLD
from zipwire._decompress import StreamingDecompressor, decompress
from zipwire._errors import BadZipFile, RangeRequestUnsupported
from zipwire._parser import _ReadRequired, find_eocd
from zipwire._write import write_all
from zipwire._zipstructs import EOCDRecord, LocalFileHeaderRecord

if typing.TYPE_CHECKING:
    import zipfile
    from collections.abc import Callable

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

    Args:
        reader: A :class:`~zipwire.SyncReader` that serves range
            requests for the archive.
        metadata_encoding: Codec for entry names without the UTF-8
            flag (default CP437), as in ``zipfile.ZipFile``.

    .. versionadded:: 0.6.0
       The *metadata_encoding* argument.
    """

    def __init__(self, reader: SyncReader, *, metadata_encoding: str | None = None) -> None:
        super().__init__(metadata_encoding=metadata_encoding)
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
        if file_size < EOCDRecord.FIXED_SIZE:
            raise BadZipFile("File is not a zip file")
        fetch_size = min(file_size, self._tail_read_size(file_size))
        tail_start = max(0, file_size - fetch_size)
        tail, _ = self._reader.read_range(tail_start, file_size - tail_start)
        if len(tail) != fetch_size:
            raise BadZipFile(f"Archive tail fetch returned {len(tail)} of {fetch_size} bytes")

        # Step 2: Parse EOCD
        records: dict[int, bytes] = {}
        while True:
            try:
                eocd = find_eocd(tail, file_size, records=records)
                break
            except _ReadRequired as request:
                data, _ = self._reader.read_range(request.offset, request.length)
                if len(data) != request.length:
                    raise BadZipFile("Truncated ZIP64 end record") from None
                records[request.offset] = data

        self._validate_cd_bounds(file_size, eocd)

        # Step 3: Reuse the tail if it contains the complete directory.
        cd_data = b""
        if eocd.cd_size:
            if eocd.cd_offset >= tail_start:
                start = eocd.cd_offset - tail_start
                cd_data = tail[start : start + eocd.cd_size]
            else:
                cd_data, _ = self._reader.read_range(eocd.cd_offset, eocd.cd_size)
        if len(cd_data) != eocd.cd_size:
            raise BadZipFile("Central directory is truncated")
        self._set_entries(cd_data, file_size, eocd)

        self._store_tail(tail_start, tail)

    def _resolve_entry(
        self,
        info: zipfile.ZipInfo,
    ) -> tuple[zipfile.ZipInfo, int, bytes | None]:
        """Validate the local header and optionally prefetch a small member."""
        fetch_size = LocalFileHeaderRecord.FIXED_SIZE + PREFETCH_EXTRA
        if info.compress_size <= PREFETCH_THRESHOLD:
            fetch_size += info.compress_size
        fetch_size = min(fetch_size, self.file_size - info.header_offset)
        buf, _ = self._reader.read_range(info.header_offset, fetch_size)
        header = LocalFileHeaderRecord.parse(buf)
        filename_end = self._check_local_header(info, header)
        if len(buf) < filename_end:
            filename, _ = self._reader.read_range(
                info.header_offset + LocalFileHeaderRecord.FIXED_SIZE, header.filename_length
            )
            if len(filename) != header.filename_length:
                raise BadZipFile(
                    f"{info.filename!r}: local header filename fetch returned "
                    f"{len(filename)} of {header.filename_length} bytes"
                )
            buf = buf[: LocalFileHeaderRecord.FIXED_SIZE] + filename
            header = replace(header, filename=filename)
        data_offset = self._validate_local_header(info, header)
        start = data_offset - info.header_offset
        if not info.compress_size or start + info.compress_size <= len(buf):
            return info, data_offset, buf[start : start + info.compress_size]
        return info, data_offset, None

    def read(
        self,
        name: str | zipfile.ZipInfo,
        pwd: bytes | None = None,
        *,
        max_file_size: int | None = None,
    ) -> bytes:
        """Read and decompress a file from the archive.

        Args:
            name: Filename string or ZipInfo object.
            pwd: Password for traditional ZIP encryption; defaults to setpassword().
            max_file_size: Optional limit on uncompressed size in bytes.
                Raises :exc:`~zipwire.FileTooLarge` if the entry's
                ``file_size`` exceeds this limit.

        Returns:
            The decompressed file contents.

        .. warning::

            This method loads the entire decompressed file into memory.
            The declared size is checked against *max_file_size* and
            output is limited to the declared size, as in zipfile.
            Use :meth:`read_into` to avoid buffering the
            complete compressed and decompressed contents.
        """
        info = self._entry_info(name, max_file_size=max_file_size)
        info, data_offset, prefetched = self._resolve_entry(info)
        password, encryption_check = self._password(info, pwd)
        if prefetched is None:
            prefetched, _ = self._reader.read_range(data_offset, info.compress_size)
        return decompress(
            prefetched,
            info.compress_type,
            info.file_size,
            info.CRC,
            filename=info.orig_filename,
            compressed_size=info.compress_size,
            pwd=password,
            encryption_check=encryption_check,
        )

    def read_into(
        self,
        name: str | zipfile.ZipInfo,
        dest: Writable,
        *,
        pwd: bytes | None = None,
        max_file_size: int | None = None,
    ) -> None:
        """Decompress a file from the archive into a writable destination.

        Unlike :meth:`read`, this truly streams: the compressed payload is
        fetched in chunks via :meth:`stream_range` and each chunk is
        decompressed and written immediately, keeping peak memory low.

        Output is limited to the declared size, as in zipfile, and checked
        against the entry's CRC. *max_file_size* limits the declared size
        before any member data is fetched.
        Compressed members continue decoding to validate the encoded data,
        discarding excess output in bounded chunks.

        Args:
            name: Filename string or ZipInfo object.
            pwd: Password for traditional ZIP encryption; defaults to setpassword().
            dest: A writable file-like object (e.g. ``io.BytesIO``, open file).
            max_file_size: Optional limit on uncompressed size in bytes.
                Raises :exc:`~zipwire.FileTooLarge` if the entry's
                ``file_size`` exceeds this limit.
        """
        info = self._entry_info(name, max_file_size=max_file_size)
        info, data_offset, prefetched = self._resolve_entry(info)
        password, encryption_check = self._password(info, pwd)
        sd = StreamingDecompressor(
            info.compress_type,
            info.CRC,
            filename=info.orig_filename,
            max_output_size=max_file_size,
            expected_size=info.file_size,
            compressed_size=info.compress_size,
            pwd=password,
            encryption_check=encryption_check,
        )
        if prefetched is not None:
            write_all(dest, sd.feed_chunks(prefetched))
        else:
            stream = self._reader.stream_range(data_offset, info.compress_size)
            with ExitStack() as cleanup:
                close: Callable[[], object] | None = getattr(stream, "close", None)
                if close is not None:
                    cleanup.callback(close)
                for chunk in stream:
                    write_all(dest, sd.feed_chunks(chunk))
                    if sd.eof:
                        break
        sd.finish()
