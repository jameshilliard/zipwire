"""Asynchronous remote ZIP reader."""

from __future__ import annotations

import typing
from contextlib import aclosing

from zipwire._base import RemoteZipBase
from zipwire._constants import PREFETCH_EXTRA, PREFETCH_THRESHOLD
from zipwire._decompress import StreamingDecompressor, adecompress
from zipwire._errors import BadZipFile, FileTooLarge, RangeRequestUnsupported
from zipwire._parser import find_eocd
from zipwire._write import awrite_all
from zipwire._zipinfo import RemoteZipInfo
from zipwire._zipstructs import LocalFileHeaderRecord

if typing.TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from zipwire._types import AsyncReader, AsyncWritable, Writable


class AsyncRemoteZip(RemoteZipBase):
    """Read files from a remote ZIP archive using async HTTP range requests.

    Must be used as an async context manager.  The archive is loaded on
    entry and all internal state is cleared on exit.  Not thread-safe.

    Usage::

        async with AsyncRemoteZip(reader) as rz:
            for info in rz.infolist():
                print(info.filename)
            data = await rz.read("path/to/file.txt")

    Args:
        reader: An :class:`~zipwire.AsyncReader` that serves range
            requests for the archive.
        metadata_encoding: Codec for entry names without the UTF-8
            flag (default CP437), as in ``zipfile.ZipFile``.

    .. versionadded:: 0.6.0
       The *metadata_encoding* argument.
    """

    def __init__(self, reader: AsyncReader, *, metadata_encoding: str | None = None) -> None:
        super().__init__(metadata_encoding=metadata_encoding)
        self._reader = reader

    async def __aenter__(self) -> typing.Self:
        try:
            await self._ensure_loaded()
        except BaseException:
            await self.close()
            raise
        return self

    async def __aexit__(self, *exc: object) -> None:
        self._clear()
        await self.close()

    async def close(self) -> None:
        """Close the underlying reader."""
        await self._reader.close()

    @property
    def url(self) -> str:
        """The target URL of the underlying reader."""
        return self._reader.url

    async def _ensure_loaded(self) -> None:
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
        head_headers = await self._reader.head()
        cl = head_headers.get("content-length")
        if cl is None:
            raise RangeRequestUnsupported("Server did not return a Content-Length header")
        file_size = int(cl)
        fetch_size = min(file_size, self._tail_read_size(file_size))
        tail_start = max(0, file_size - fetch_size)
        tail, _ = await self._reader.read_range(tail_start, file_size - tail_start)
        if len(tail) != file_size - tail_start:
            raise BadZipFile(
                f"Archive tail fetch returned {len(tail)} of {file_size - tail_start} bytes"
            )

        # Step 2: Parse EOCD
        eocd = find_eocd(tail, file_size)

        # Step 3: Fetch central directory and store parsed state
        self._validate_cd_bounds(file_size, eocd)
        cd_data, _ = await self._reader.read_range(eocd.cd_offset, eocd.cd_size)
        self._set_entries(cd_data, file_size, eocd)
        self._store_tail(tail_start, tail)

    async def _resolve_entry(
        self,
        name: str | RemoteZipInfo,
    ) -> tuple[RemoteZipInfo, int, bytes | None] | None:
        """Resolve *name* to a ZipInfo and compute the compressed data offset.

        The local file header's filename is validated against the
        central directory name, as ``zipfile.ZipFile.open()`` does.

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
            fetch_size = LocalFileHeaderRecord.FIXED_SIZE + PREFETCH_EXTRA + info.compress_size
            fetch_size = min(fetch_size, self.file_size - info.header_offset)
            buf, _ = await self._reader.read_range(info.header_offset, fetch_size)
            local_header = LocalFileHeaderRecord.parse(buf)
            data_start = LocalFileHeaderRecord.FIXED_SIZE + local_header.data_offset_past_header
            data_offset = info.header_offset + data_start
            if data_start + info.compress_size <= len(buf):
                self._validate_data_region(info, data_offset)
                self._check_local_filename(info, local_header.filename)
                return info, data_offset, buf[data_start : data_start + info.compress_size]
            # Rare: variable fields exceeded PREFETCH_EXTRA, fall through
        else:
            # Large file: fetch the header plus an allowance for its
            # variable fields so the filename can be validated; the
            # compressed data is fetched or streamed separately.
            fetch_size = min(
                LocalFileHeaderRecord.FIXED_SIZE + PREFETCH_EXTRA,
                self.file_size - info.header_offset,
            )
            buf, _ = await self._reader.read_range(info.header_offset, fetch_size)
            local_header = LocalFileHeaderRecord.parse(buf)
            data_offset = (
                info.header_offset
                + LocalFileHeaderRecord.FIXED_SIZE
                + local_header.data_offset_past_header
            )

        self._validate_data_region(info, data_offset)
        self._check_local_filename(info, await self._fetch_local_filename(info, local_header))
        return info, data_offset, None

    async def _fetch_local_filename(
        self,
        info: RemoteZipInfo,
        local_header: LocalFileHeaderRecord,
    ) -> bytes | None:
        """Return the raw local filename, fetching it if it was not prefetched.

        ``_validate_data_region()`` must have passed for *info* first:
        it bounds the local header's filename length by the archive
        end, so the fetch here cannot go past it.

        Raises:
            BadZipFile: If the fetch returns fewer bytes than the local
                header's filename length.
        """
        if local_header.filename is not None:
            return local_header.filename
        # Rare: the local filename exceeds the PREFETCH_EXTRA allowance.
        offset = info.header_offset + LocalFileHeaderRecord.FIXED_SIZE
        filename, _ = await self._reader.read_range(offset, local_header.filename_length)
        if len(filename) != local_header.filename_length:
            raise BadZipFile(
                f"{info.filename!r}: local header filename fetch returned "
                f"{len(filename)} of {local_header.filename_length} bytes"
            )
        return filename

    async def read(
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
            A truncated stream raises :exc:`~zipwire.TruncatedStream`,
            and a decompressed size that differs from the declared
            ``file_size`` raises :exc:`~zipwire.SizeMismatch`.  A local
            file header whose filename differs from the central
            directory name raises :exc:`~zipwire.BadZipFile`, as
            ``zipfile.ZipFile.open()`` does.
        """
        resolved = await self._resolve_entry(name)
        if resolved is None:
            return b""
        info, data_offset, prefetched = resolved
        if max_file_size is not None and info.file_size > max_file_size:
            raise FileTooLarge(info.filename, info.file_size, max_file_size)
        if prefetched is None:
            prefetched, _ = await self._reader.read_range(data_offset, info.compress_size)
            if len(prefetched) != info.compress_size:
                raise BadZipFile(
                    f"{info.filename!r}: data fetch returned {len(prefetched)} "
                    f"of {info.compress_size} bytes"
                )
        return await adecompress(
            prefetched,
            info.compress_type,
            info.file_size,
            info.CRC,
        )

    async def read_into(
        self,
        name: str | RemoteZipInfo,
        dest: Writable | AsyncWritable,
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

        The streamed response is closed when extraction finishes, fails,
        or is cancelled.  If closing the stream raises, the close error
        propagates and may mask the original exception.

        If ``dest.write()`` returns an awaitable, it is awaited before the
        next chunk is read or decompressed, so a slow destination applies
        backpressure to the transfer.  Synchronous destinations such as
        :class:`io.BytesIO` are supported, too.  Their writes run on the
        event-loop thread.

        Writes are checked: a destination that accepts fewer bytes than
        given is retried with the remaining bytes, while a byte count that
        is not an :class:`int`, or is zero, negative, or oversized, raises
        :exc:`OSError`.  A ``write()`` that returns ``None`` is assumed to
        have accepted the whole chunk.

        The caller owns *dest*: this method does not flush, close, or
        commit it.  Errors and cancellation may leave partial output; only
        a successful return indicates that the stream ended cleanly and
        the CRC-32 and size checks passed.

        A local file header whose filename differs from the central
        directory name raises :exc:`~zipwire.BadZipFile`, as
        ``zipfile.ZipFile.open()`` does.

        .. versionchanged:: 0.5.0

           The response is now closed deterministically instead of relying
           on async-generator finalization, and *dest* may be an
           asynchronous writable object.

        Args:
            name: Filename string or RemoteZipInfo object.
            dest: A synchronous or asynchronous writable object, such as
                ``io.BytesIO``, an open file, or ``anyio.AsyncFile``.
            max_file_size: Optional limit on uncompressed size in bytes.
                Raises :exc:`~zipwire.FileTooLarge` if the entry's
                ``file_size`` exceeds this limit, or if the actual
                decompressed output exceeds it.
        """
        resolved = await self._resolve_entry(name)
        if resolved is None:
            return
        info, data_offset, prefetched = resolved
        if max_file_size is not None and info.file_size > max_file_size:
            raise FileTooLarge(info.filename, info.file_size, max_file_size)
        outputs = self._output_chunks(info, data_offset, prefetched, max_file_size)
        async with aclosing(outputs) as chunks:
            async for output in chunks:
                await awrite_all(dest, (output,))

    async def _output_chunks(
        self,
        info: RemoteZipInfo,
        data_offset: int,
        prefetched: bytes | None,
        max_file_size: int | None,
    ) -> AsyncGenerator[bytes, None]:
        """Decompress one entry and yield its output chunks.

        Output chunks are bounded by the decompressor, and empty chunks
        mark decoding steps that produced no bytes.  The compressed
        payload is streamed from the reader; the streamed response is
        closed as soon as the generator is closed, whether it finished,
        failed, or was cancelled.
        """
        sd = StreamingDecompressor(
            info.compress_type,
            info.CRC,
            filename=info.filename,
            max_output_size=max_file_size,
            expected_size=info.file_size,
        )
        if prefetched is not None:
            for output in sd.feed_chunks(prefetched):
                yield output
        else:
            stream = self._reader.stream_range(data_offset, info.compress_size)
            async with aclosing(stream) as chunks:
                async for chunk in chunks:
                    for output in sd.feed_chunks(chunk):
                        yield output
        sd.finish()
