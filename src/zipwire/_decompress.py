"""Decompression and CRC verification for ZIP entries."""

from __future__ import annotations

import asyncio
import io
import typing
import zlib

from zipwire._constants import STREAM_CHUNK_SIZE, CompressionMethod
from zipwire._errors import (
    CRCMismatch,
    FileTooLarge,
    SizeMismatch,
    TruncatedStream,
    UnsupportedCompression,
)

if typing.TYPE_CHECKING:
    from collections.abc import Iterator


class Decompressor(typing.Protocol):
    @property
    def eof(self) -> bool: ...

    def decompress(self, data: bytes, max_length: int, /) -> bytes: ...


class DeflateDecompressor(Decompressor, typing.Protocol):
    @property
    def unconsumed_tail(self) -> bytes: ...


class IncrementalDecompressor(Decompressor, typing.Protocol):
    @property
    def needs_input(self) -> bool: ...


def decompress(
    data: bytes,
    method: int,
    expected_size: int,
    expected_crc: int,
) -> bytes:
    """Decompress data and verify CRC-32.

    Args:
        data: The raw (possibly compressed) file data.
        method: ZIP compression method (0=stored, 8=deflated, 12=bzip2, 14=lzma, 93=zstandard).
        expected_size: Expected uncompressed size, verified against the
            actual decompressed length.
        expected_crc: Expected CRC-32 of the uncompressed data.

    Returns:
        The decompressed bytes.

    Raises:
        UnsupportedCompression: If the compression method is not supported.
        CRCMismatch: If the CRC-32 check fails.
        SizeMismatch: If the decompressed size differs from *expected_size*.
    """
    match method:
        case CompressionMethod.STORED:
            result = data
        case CompressionMethod.DEFLATED:
            # -15 = raw deflate (no zlib/gzip header)
            result = zlib.decompress(data, -15)
        case CompressionMethod.BZIP2:
            try:
                import bz2
            except ImportError:  # pragma: no cover
                raise UnsupportedCompression(method) from None

            result = bz2.decompress(data)
        case CompressionMethod.LZMA:
            try:
                import lzma
            except ImportError:  # pragma: no cover
                raise UnsupportedCompression(method) from None

            result = lzma.decompress(data)
        case CompressionMethod.ZSTANDARD:
            try:
                from compression.zstd import decompress as _zstd_decompress
            except ImportError:  # pragma: no cover
                raise UnsupportedCompression(method) from None

            result = _zstd_decompress(data)
        case _:
            raise UnsupportedCompression(method)

    actual_crc = zlib.crc32(result) & 0xFFFFFFFF
    if actual_crc != expected_crc:
        raise CRCMismatch(expected_crc, actual_crc)
    if len(result) != expected_size:
        raise SizeMismatch(expected_size, len(result))

    return result


async def adecompress(
    data: bytes,
    method: int,
    expected_size: int,
    expected_crc: int,
) -> bytes:
    """Decompress data with scheduling checkpoints between bounded steps.

    Same result as :func:`decompress` -- including the CRC-32 and size
    checks and the exceptions they raise -- but the work is split into
    chunks of at most 2 MiB of decompressed output and the event loop
    runs between them.
    """
    if method == CompressionMethod.STORED:
        actual_crc = 0
        with memoryview(data) as view:
            for offset in range(0, len(data), STREAM_CHUNK_SIZE):
                actual_crc = zlib.crc32(view[offset : offset + STREAM_CHUNK_SIZE], actual_crc)
                await asyncio.sleep(0)  # yield to loop
        if actual_crc != expected_crc:
            raise CRCMismatch(expected_crc, actual_crc)
        if len(data) != expected_size:
            raise SizeMismatch(expected_size, len(data))
        return data

    output = io.BytesIO()
    sd = StreamingDecompressor(method, expected_crc, expected_size=expected_size)
    for chunk in sd.feed_chunks(data):
        output.write(chunk)
        await asyncio.sleep(0)  # yield to loop
    sd.finish()
    return output.getvalue()


class StreamingDecompressor:
    """Incrementally decompress data and return the output to the caller.

    :meth:`feed_chunks` yields decoded output in bounded chunks, never
    larger than 2 MiB, so a single giant entry cannot inflate into one
    enormous allocation.  It also yields empty chunks for decoding steps
    that produced no output; async callers can turn those steps into
    scheduling checkpoints.  :meth:`feed` collects a feed's output into
    bytes for callers that do not need bounded output buffering.  Call
    :meth:`finish` after the last input chunk; it verifies that a
    compressed stream reached its end and checks CRC-32 and, when given,
    the expected output size.

    Feeding non-empty data after the stream has ended raises
    :exc:`~zipwire.TruncatedStream`.

    When *max_output_size* is set, the cumulative decompressed output is
    tracked and :exc:`~zipwire.FileTooLarge` is raised if it exceeds the
    limit.  When *expected_size* is set, output beyond it is rejected as
    soon as it is produced, and :meth:`finish` rejects a total output
    that falls short of it, both with :exc:`~zipwire.SizeMismatch`.
    *max_output_size* takes precedence when a chunk exceeds both.
    """

    def __init__(
        self,
        method: int,
        expected_crc: int,
        *,
        filename: str = "",
        max_output_size: int | None = None,
        expected_size: int | None = None,
    ) -> None:
        self._expected_crc = expected_crc
        self._expected_size = expected_size
        self._crc: int = 0
        self._output_size: int = 0
        self._filename = filename
        self._max_output_size = max_output_size
        self.eof = False
        self._stream_ended = False
        self._deflate: DeflateDecompressor | None = None
        self._incremental: IncrementalDecompressor | None = None

        match method:
            case CompressionMethod.STORED:
                pass
            case CompressionMethod.DEFLATED:
                self._deflate = zlib.decompressobj(-15)
            case CompressionMethod.BZIP2:
                try:
                    import bz2
                except ImportError:  # pragma: no cover
                    raise UnsupportedCompression(method) from None

                self._incremental = bz2.BZ2Decompressor()
            case CompressionMethod.LZMA:
                try:
                    import lzma
                except ImportError:  # pragma: no cover
                    raise UnsupportedCompression(method) from None

                self._incremental = lzma.LZMADecompressor()
            case CompressionMethod.ZSTANDARD:
                try:
                    from compression.zstd import ZstdDecompressor
                except ImportError:  # pragma: no cover
                    raise UnsupportedCompression(method) from None

                self._incremental = ZstdDecompressor()
            case _:
                raise UnsupportedCompression(method)

        self._stored = self._deflate is None and self._incremental is None

    def _check_output(self, chunk: bytes) -> bytes:
        """Account for decompressed bytes and enforce the size limits."""
        if not chunk:
            return b""
        self._output_size += len(chunk)
        if self._max_output_size is not None and self._output_size > self._max_output_size:
            raise FileTooLarge(self._filename, self._output_size, self._max_output_size)
        if self._expected_size is not None and self._output_size > self._expected_size:
            raise SizeMismatch(self._expected_size, self._output_size)
        self._crc = zlib.crc32(chunk, self._crc)
        return chunk

    def _check_open(self, data: bytes) -> None:
        """Reject non-empty input once the stream has ended."""
        if self.eof and data:
            raise TruncatedStream(
                f"{self._filename!r}: {len(data)} bytes after end of compressed stream"
            )

    def feed(self, data: bytes) -> bytes:
        """Decompress *data* and return the output after checking its size."""
        if self._stored:
            # The input is already the decoded output; return it without
            # the slice-and-join round trip that feed_chunks() would do.
            self._check_open(data)
            return self._check_output(data)
        return b"".join(self.feed_chunks(data))

    def feed_chunks(self, data: bytes) -> Iterator[bytes]:
        """Decompress *data* and yield bounded output chunks.

        Empty chunks mark decoding steps that produced no output; they
        are still useful as scheduling checkpoints.  Raises
        :exc:`~zipwire.TruncatedStream` when *data* is non-empty and the
        stream has already ended.
        """
        if self.eof:
            self._check_open(data)
            return
        for offset in range(0, len(data), STREAM_CHUNK_SIZE):
            chunk = data[offset : offset + STREAM_CHUNK_SIZE]
            yield from self._decode_chunks(chunk)
            if self.eof:
                break

    def _decode_chunks(self, data: bytes) -> Iterator[bytes]:
        decoder = self._deflate if self._deflate is not None else self._incremental
        if decoder is None:
            yield self._check_output(data)
            return
        while True:
            chunk = decoder.decompress(data, STREAM_CHUNK_SIZE)
            yield self._check_output(chunk)
            self.eof = decoder.eof
            if self.eof:
                self._stream_ended = True
                break
            if self._deflate is not None:
                data = self._deflate.unconsumed_tail
                if not data and len(chunk) < STREAM_CHUNK_SIZE:
                    break
            else:
                assert self._incremental is not None
                if self._incremental.needs_input:
                    break
                data = b""

    def finish(self) -> None:
        """Verify that the stream ended cleanly, then check size and CRC-32.

        Raises :exc:`~zipwire.TruncatedStream` when a compressed stream
        never reached its end, :exc:`~zipwire.CRCMismatch` when the
        CRC-32 does not match, and :exc:`~zipwire.SizeMismatch` when
        *expected_size* was given and the total output falls short of it
        (output beyond *expected_size* is already rejected while
        feeding).
        """
        self.eof = True
        if not self._stored and not self._stream_ended:
            raise TruncatedStream(f"{self._filename!r}: compressed stream is truncated")
        actual_crc = self._crc & 0xFFFFFFFF
        if actual_crc != self._expected_crc:
            raise CRCMismatch(self._expected_crc, actual_crc)
        if self._expected_size is not None and self._output_size != self._expected_size:
            raise SizeMismatch(self._expected_size, self._output_size)
