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
    import lzma
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


class ZipLZMADecompressor:
    """Incremental decoder for ZIP-LZMA (method 14) compressed data.

    Buffers the ZIP-LZMA header from the first input bytes -- two
    version bytes, a two-byte little-endian property size, then that
    many bytes of LZMA1 properties -- and decodes the remaining raw
    LZMA1 stream.  The version bytes are deliberately not validated:
    the ZIP specification says 1.0 while stdlib :mod:`zipfile` writes
    9.4, and the properties fully determine the decoder.

    Implements the :class:`IncrementalDecompressor` protocol.
    """

    # Fixed part of the ZIP-LZMA payload header: 2 version bytes followed
    # by a 2-byte little-endian size of the LZMA1 properties that follow.
    _FIXED_HEADER_SIZE = 4

    # The largest dictionary a real encoder writes into a ZIP is 64 MiB
    # (xz and 7-Zip "ultra" mode, preset 9); stdlib zipfile writes
    # 8 MiB.  A larger claim in the properties comes from a hostile or
    # broken archive and is clamped, so a crafted entry cannot reserve
    # absurd address space and commit charge.  Clamping cannot corrupt
    # decoding: an LZMA match never references farther back than the
    # output decoded so far, and a stream that does need more fails
    # with a clean "Corrupt input data" error.
    _MAX_DICT_SIZE = 64 * 1024 * 1024

    def __init__(self) -> None:
        try:
            import lzma
        except ImportError:  # pragma: no cover
            raise UnsupportedCompression(CompressionMethod.LZMA) from None

        self._lzma = lzma
        self._decoder: lzma.LZMADecompressor | None = None
        self._pending = bytearray()
        self.eof = False

    @property
    def needs_input(self) -> bool:
        # While the header is still incomplete, more input is required;
        # afterwards defer to the wrapped decoder so a bounded
        # decompress() call can be drained across reads.
        if self._decoder is None:
            return True
        return self._decoder.needs_input

    @property
    def header_pending(self) -> bool:
        """Whether the ZIP-LZMA header is still incomplete."""
        return self._decoder is None

    def _lzma1_filter(self, props: bytes) -> dict[str, int]:
        """Translate ZIP-LZMA properties into a raw LZMA1 filter spec.

        A ZIP-LZMA (method 14) payload is not an xz or ``.lzma``
        container, which is why ``lzma.decompress()`` and a plain
        ``lzma.LZMADecompressor()`` reject it.  After the header it
        holds a raw LZMA1 stream, which needs a ``FORMAT_RAW`` decoder
        configured from the properties.

        Security: *props* comes from the remote archive and is
        untrusted.  The lc/lp/pb encoding and the property length are
        validated here and the dictionary size claim is clamped to
        :attr:`_MAX_DICT_SIZE`; everything else is left to liblzma,
        which rejects values it cannot use before any decoding happens.
        The decoded output itself stays bounded by the CRC and size
        verification.
        """
        lzma = self._lzma
        if len(props) != 5:
            raise lzma.LZMAError(f"invalid ZIP-LZMA properties size: {len(props)}")
        encoded = props[0]
        if encoded >= 9 * 5 * 5:  # lc < 9, lp < 5, pb < 5
            raise lzma.LZMAError("invalid ZIP-LZMA lc/lp/pb properties")
        pb, rest = divmod(encoded, 45)
        lp, lc = divmod(rest, 9)
        dict_size = min(int.from_bytes(props[1:5], "little"), self._MAX_DICT_SIZE)
        return {
            "id": lzma.FILTER_LZMA1,
            "lc": lc,
            "lp": lp,
            "pb": pb,
            "dict_size": dict_size,
        }

    def decompress(self, data: bytes, max_length: int = -1) -> bytes:
        if self._decoder is None:
            self._pending += data
            if len(self._pending) < self._FIXED_HEADER_SIZE:
                return b""
            props_size = int.from_bytes(self._pending[2:4], "little")
            if len(self._pending) < self._FIXED_HEADER_SIZE + props_size:
                return b""
            props = bytes(self._pending[4 : 4 + props_size])
            self._decoder = self._lzma.LZMADecompressor(
                self._lzma.FORMAT_RAW,
                filters=[self._lzma1_filter(props)],
            )
            data = bytes(self._pending[4 + props_size :])
            self._pending.clear()
        result = self._decoder.decompress(data, max_length)
        self.eof = self._decoder.eof
        return result


def _decompress_zip_lzma(data: bytes) -> bytes:
    """Decode a complete ZIP-LZMA payload in one shot."""
    try:
        import lzma
    except ImportError:  # pragma: no cover
        raise UnsupportedCompression(CompressionMethod.LZMA) from None

    decoder = ZipLZMADecompressor()
    result = decoder.decompress(data)
    if decoder.header_pending:
        raise lzma.LZMAError("ZIP-LZMA header is truncated")
    if not decoder.eof:
        raise lzma.LZMAError("compressed data ended before the end-of-stream marker")
    return result


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
            result = _decompress_zip_lzma(data)
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
                self._incremental = ZipLZMADecompressor()
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
