"""Asynchronous remote ZIP reader."""

from __future__ import annotations

import asyncio
import typing
from contextlib import aclosing
from inspect import isawaitable

from zipwire._base import RemoteZipBase
from zipwire._constants import (
    LOCAL_FILE_HEADER_SIZE,
    PREFETCH_EXTRA,
    PREFETCH_THRESHOLD,
)
from zipwire._decompress import StreamingDecompressor, adecompress
from zipwire._errors import FileTooLarge, RangeRequestUnsupported
from zipwire._parser import find_eocd, parse_local_file_header
from zipwire._zipinfo import RemoteZipInfo

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
    """

    def __init__(self, reader: AsyncReader) -> None:
        super().__init__()
        self._reader = reader

    async def __aenter__(self) -> AsyncRemoteZip:
        await self._ensure_loaded()
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

    async def _ensure_loaded(self) -> tuple[int, bytes]:
        """Fetch and parse the central directory.

        Returns:
            ``(tail_start, tail_data)`` -- the offset and bytes of the
            initial tail fetch.

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

        # Step 2: Parse EOCD
        eocd = find_eocd(tail, file_size)

        # Step 3: Fetch central directory and store parsed state
        cd_data, _ = await self._reader.read_range(eocd.cd_offset, eocd.cd_size)
        self._set_entries(cd_data, file_size, eocd)

        return tail_start, tail

    async def _resolve_entry(
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

        if info.compress_size <= PREFETCH_THRESHOLD:
            # Small file: fetch local header + compressed data in one request.
            fetch_size = LOCAL_FILE_HEADER_SIZE + PREFETCH_EXTRA + info.compress_size
            buf, _ = await self._reader.read_range(info.header_offset, fetch_size)
            local_header = parse_local_file_header(buf)
            data_start = LOCAL_FILE_HEADER_SIZE + local_header.data_offset_past_header
            data_offset = info.header_offset + data_start
            if data_start + info.compress_size <= len(buf):
                return info, data_offset, buf[data_start : data_start + info.compress_size]
            # Rare: variable fields exceeded PREFETCH_EXTRA, fall through
        else:
            buf, _ = await self._reader.read_range(info.header_offset, LOCAL_FILE_HEADER_SIZE)
            local_header = parse_local_file_header(buf)
            data_offset = (
                info.header_offset + LOCAL_FILE_HEADER_SIZE + local_header.data_offset_past_header
            )

        return info, data_offset, None

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
            ``file_size`` raises :exc:`~zipwire.SizeMismatch`.
        """
        resolved = await self._resolve_entry(name)
        if resolved is None:
            return b""
        info, data_offset, prefetched = resolved
        if max_file_size is not None and info.file_size > max_file_size:
            raise FileTooLarge(info.filename, info.file_size, max_file_size)
        if prefetched is None:
            prefetched, _ = await self._reader.read_range(data_offset, info.compress_size)
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
        or is cancelled.

        If ``dest.write()`` returns an awaitable, it is awaited before the
        next chunk is read or decompressed, so a slow destination applies
        backpressure to the transfer.  Synchronous destinations such as
        :class:`io.BytesIO` are supported, too.  Their writes run on the
        event-loop thread. Each write must accept the whole chunk or raise
        an exception; a short write is not retried.

        The caller owns *dest*: this method does not flush, close, or
        commit it.  Errors and cancellation may leave partial output; only
        a successful return indicates that the stream ended cleanly and
        the CRC-32 and size checks passed.

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
                if not output:
                    await asyncio.sleep(0)  # yield to loop
                    continue
                written = dest.write(output)
                # Sync file writes return the byte count; skip the costlier
                # awaitable probe for the common case.
                if type(written) is not int and isawaitable(written):
                    await written
                await asyncio.sleep(0)  # yield to loop

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
