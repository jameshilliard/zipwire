"""Asynchronous remote ZIP reader."""

from __future__ import annotations

import typing
from contextlib import AsyncExitStack
from inspect import isawaitable

from zipwire._base import RemoteZipBase
from zipwire._constants import (
    LOCAL_FILE_HEADER_SIZE,
    PREFETCH_EXTRA,
    PREFETCH_THRESHOLD,
)
from zipwire._decompress import StreamingDecompressor, decompress
from zipwire._errors import FileTooLarge, RangeRequestUnsupported
from zipwire._parser import find_eocd, parse_local_file_header
from zipwire._zipinfo import RemoteZipInfo

if typing.TYPE_CHECKING:
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
        """
        resolved = await self._resolve_entry(name)
        if resolved is None:
            return b""
        info, data_offset, prefetched = resolved
        if max_file_size is not None and info.file_size > max_file_size:
            raise FileTooLarge(info.filename, info.file_size, max_file_size)
        if prefetched is None:
            prefetched, _ = await self._reader.read_range(data_offset, info.compress_size)
        return decompress(
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

        The streaming decompressor also enforces *max_file_size* during
        decompression, aborting if the actual output exceeds the limit
        regardless of what the entry metadata claims.

        If ``dest.write()`` returns an awaitable, it is awaited before
        further input is read or decompressed. Synchronous destinations
        are supported too; their writes run on the event-loop thread.
        Each write must accept the entire chunk or raise an exception.

        The caller owns *dest*: this method does not flush, close, or
        commit it. Errors and cancellation may leave partial output;
        only a successful return indicates that CRC verification passed.

        Args:
            name: Filename string or RemoteZipInfo object.
            dest: A synchronous or asynchronous writable object, such as
                ``io.BytesIO`` or ``anyio.AsyncFile``.
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
        sd = StreamingDecompressor(
            info.compress_type,
            info.CRC,
            filename=info.filename,
            max_output_size=max_file_size,
        )
        if prefetched is not None:
            await _write_output(dest, sd.feed(prefetched))
        else:
            async with AsyncExitStack() as stack:
                stream = self._reader.stream_range(data_offset, info.compress_size)
                # AsyncReader permits any async iterator. Register cleanup
                # when it supports aclose(), as all built-in backends do.
                if close := getattr(stream, "aclose", None):
                    stack.push_async_callback(close)
                async for chunk in stream:
                    await _write_output(dest, sd.feed(chunk))
        await _write_output(dest, sd.finish())


async def _write_output(dest: Writable | AsyncWritable, data: bytes) -> None:
    """Finish one nonempty write before the caller produces more output."""
    if data:
        result = dest.write(data)
        if isawaitable(result):
            await result
