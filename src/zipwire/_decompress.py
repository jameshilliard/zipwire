"""Incremental ZIP decompression, decryption, and CRC verification."""

from __future__ import annotations

import io
import typing

from anyio.lowlevel import checkpoint

try:
    import zlib
    from zlib import crc32
except ImportError:  # pragma: no cover
    from binascii import crc32

    zlib = None  # type: ignore[assignment]

try:
    import bz2
except ImportError:  # pragma: no cover
    bz2 = None  # type: ignore[assignment]

try:
    import lzma
except ImportError:  # pragma: no cover
    lzma = None  # type: ignore[assignment]

try:
    from compression import zstd
except ImportError:  # pragma: no cover
    zstd = None

from zipwire._constants import STREAM_CHUNK_SIZE, CompressionMethod
from zipwire._crypto import ZipCrypto
from zipwire._errors import (
    BadZipFile,
    CRCMismatch,
    FileTooLarge,
    SizeMismatch,
    TruncatedStream,
    UnsupportedCompression,
)
from zipwire._write import awrite_all, write_all

if typing.TYPE_CHECKING:
    from collections.abc import Iterator
    from lzma import LZMADecompressor

# ZipCrypto runs in Python, so use smaller steps than the native codecs.
_DECRYPT_CHUNK_SIZE = 16 * 1024


class _Decompressor(typing.Protocol):
    @property
    def eof(self) -> bool: ...

    def decompress(self, data: bytes, max_length: int, /) -> bytes: ...


class _DeflateDecompressor(_Decompressor, typing.Protocol):
    @property
    def unconsumed_tail(self) -> bytes: ...


class _IncrementalDecompressor(_Decompressor, typing.Protocol):
    @property
    def needs_input(self) -> bool: ...


class ZipLZMADecompressor:
    """Decode ZIP's properties header before feeding raw LZMA1 data."""

    _MAX_DICT_SIZE = 64 * 1024 * 1024

    def __init__(self) -> None:
        self._header = bytearray()
        self._decoder: LZMADecompressor | None = None

    @property
    def eof(self) -> bool:
        return self._decoder is not None and self._decoder.eof

    @property
    def needs_input(self) -> bool:
        return self._decoder is None or self._decoder.needs_input

    def decompress(self, data: bytes, max_length: int) -> bytes:
        if self._decoder is None:
            # ZIP method 14 uses a 4-byte prefix and the 5-byte LZMA1
            # properties block, not the .xz/.lzma container formats.
            if len(self._header) < 4:
                needed = 4 - len(self._header)
                self._header.extend(data[:needed])
                data = data[needed:]
                if len(self._header) < 4:
                    return b""
            properties_size = int.from_bytes(self._header[2:4], "little")
            needed = 4 + properties_size - len(self._header)
            self._header.extend(data[:needed])
            data = data[needed:]
            # Like zipfile, validate only once the properties and at least
            # one payload byte arrive. The 16-bit size bounds buffering.
            if not data:
                return b""
            assert lzma is not None
            if properties_size != 5:
                raise lzma.LZMAError("Invalid or unsupported options")
            properties = self._header[4]
            if properties >= 225:
                raise lzma.LZMAError("Invalid or unsupported options")
            self._decoder = lzma.LZMADecompressor(
                format=lzma.FORMAT_RAW,
                filters=[
                    {
                        "id": lzma.FILTER_LZMA1,
                        "lc": properties % 9,
                        "lp": (properties // 9) % 5,
                        "pb": properties // 45,
                        "dict_size": min(
                            self._MAX_DICT_SIZE,
                            max(4096, int.from_bytes(self._header[5:9], "little")),
                        ),
                    }
                ],
            )
        return self._decoder.decompress(data, max_length)


def _stored_data(data: bytes, expected_size: int, compressed_size: int | None) -> bytes:
    """Preserve the stored-entry fast path while checking input exhaustion."""
    if compressed_size is None:
        return data
    else:
        data = data[:compressed_size]
        if len(data) < compressed_size and (not data or len(data) < expected_size):
            raise TruncatedStream("Compressed ZIP entry is truncated")
    return data[:expected_size]


def decompress(
    data: bytes,
    method: int,
    expected_size: int,
    expected_crc: int,
    *,
    filename: str = "",
    compressed_size: int | None = None,
    pwd: bytes | None = None,
    encryption_check: int | None = None,
) -> bytes:
    """Decode a complete entry using the same checks as streamed extraction."""
    if method == CompressionMethod.STORED and encryption_check is None:
        data = _stored_data(data, expected_size, compressed_size)
        actual_crc = crc32(data)
        if actual_crc != expected_crc:
            raise CRCMismatch(expected_crc, actual_crc)
        if compressed_size is None and len(data) != expected_size:
            raise SizeMismatch(expected_size, len(data))
        return data
    dest = io.BytesIO()
    decoder = StreamingDecompressor(
        method,
        expected_crc,
        filename=filename,
        expected_size=expected_size,
        compressed_size=compressed_size,
        pwd=pwd,
        encryption_check=encryption_check,
    )
    write_all(dest, decoder.feed_chunks(data))
    if method == CompressionMethod.LZMA and compressed_size is None and not decoder.eof:
        assert lzma is not None
        raise lzma.LZMAError("ZIP-LZMA stream is truncated")
    decoder.finish()
    return dest.getvalue()


async def adecompress(
    data: bytes,
    method: int,
    expected_size: int,
    expected_crc: int,
    *,
    filename: str = "",
    compressed_size: int | None = None,
    pwd: bytes | None = None,
    encryption_check: int | None = None,
) -> bytes:
    """Decode an entry with scheduling checkpoints between bounded steps."""
    if method == CompressionMethod.STORED and encryption_check is None:
        data = _stored_data(data, expected_size, compressed_size)
        actual_crc = 0
        with memoryview(data) as view:
            for offset in range(0, len(data), STREAM_CHUNK_SIZE):
                actual_crc = crc32(view[offset : offset + STREAM_CHUNK_SIZE], actual_crc)
                await checkpoint()
        if actual_crc != expected_crc:
            raise CRCMismatch(expected_crc, actual_crc)
        if compressed_size is None and len(data) != expected_size:
            raise SizeMismatch(expected_size, len(data))
        return data
    dest = io.BytesIO()
    decoder = StreamingDecompressor(
        method,
        expected_crc,
        filename=filename,
        expected_size=expected_size,
        compressed_size=compressed_size,
        pwd=pwd,
        encryption_check=encryption_check,
    )
    await awrite_all(dest, decoder.feed_chunks(data), checkpoint=checkpoint)
    decoder.finish()
    return dest.getvalue()


class StreamingDecompressor:
    """Return decoded output without owning a destination.

    ``feed_chunks()`` yields bounded chunks, including empty chunks that
    mark decoding steps with no output. Async callers can use these steps
    as scheduling checkpoints. ``feed()`` collects a feed's output into
    bytes for callers that do not need bounded output buffering.
    ``finish()`` checks input exhaustion and verifies CRC-32.

    Without ``compressed_size``, the decoded size and codec end marker
    are checked strictly. Archive readers pass the compressed extent to
    follow zipfile's output truncation and input-exhaustion rules instead.
    """

    def __init__(
        self,
        method: int,
        expected_crc: int,
        *,
        filename: str = "",
        max_output_size: int | None = None,
        expected_size: int | None = None,
        compressed_size: int | None = None,
        pwd: bytes | None = None,
        encryption_check: int | None = None,
    ) -> None:
        self._expected_crc = expected_crc
        self._crc = 0
        self._output_size = 0
        self._filename = filename
        self._max_output_size = max_output_size
        self._expected_size = expected_size
        self._output_complete = False
        self._compressed_left = compressed_size
        self._strict_size = compressed_size is None
        self.eof = False
        self._decrypter = (
            ZipCrypto(pwd) if pwd is not None and encryption_check is not None else None
        )
        self._encryption_check = encryption_check
        self._encryption_header = bytearray()
        self._deflate: _DeflateDecompressor | None = None
        self._incremental: _IncrementalDecompressor | None = None

        match method:
            case CompressionMethod.STORED:
                pass
            case CompressionMethod.DEFLATED:
                if zlib is None:
                    raise RuntimeError("Compression requires the (missing) zlib module")
                self._deflate = zlib.decompressobj(-15)
            case CompressionMethod.BZIP2:
                if bz2 is None:
                    raise RuntimeError("Compression requires the (missing) bz2 module")
                self._incremental = bz2.BZ2Decompressor()
            case CompressionMethod.LZMA:
                if lzma is None:
                    raise RuntimeError("Compression requires the (missing) lzma module")
                self._incremental = ZipLZMADecompressor()
            case CompressionMethod.ZSTANDARD | CompressionMethod.ZSTANDARD_LEGACY:
                if zstd is None:
                    raise RuntimeError(
                        "Compression requires the (missing) compression.zstd module"
                    )
                self._incremental = zstd.ZstdDecompressor()
            case _:
                raise UnsupportedCompression(method)

    def _check_output(self, chunk: bytes) -> bytes:
        if self._expected_size is not None and not self._strict_size:
            remaining = self._expected_size - self._output_size
            chunk = chunk[:remaining]
            if len(chunk) == remaining:
                self._output_complete = True
        if not chunk:
            return b""
        self._output_size += len(chunk)
        if self._max_output_size is not None and self._output_size > self._max_output_size:
            raise FileTooLarge(self._filename, self._output_size, self._max_output_size)
        if (
            self._strict_size
            and self._expected_size is not None
            and self._output_size > self._expected_size
        ):
            raise SizeMismatch(self._expected_size, self._output_size)
        self._crc = crc32(chunk, self._crc)
        return chunk

    def feed(self, data: bytes) -> bytes:
        """Consume compressed input and return its decoded output as bytes."""
        if self._deflate is None and self._incremental is None and self._decrypter is None:
            if self.eof:
                if data:
                    raise TruncatedStream("Data after end of compressed stream")
                return b""
            if self._compressed_left is not None:
                data = data[: self._compressed_left]
                self._compressed_left -= len(data)
            output = self._check_output(data)
            self.eof = self._output_complete
            return output
        return b"".join(self.feed_chunks(data))

    def feed_chunks(self, data: bytes) -> Iterator[bytes]:
        """Yield bounded output, including steps with no output bytes."""
        if self.eof:
            if data:
                raise TruncatedStream("Data after end of compressed stream")
            return
        if self._compressed_left is not None:
            data = data[: self._compressed_left]
            self._compressed_left -= len(data)
        start = 0
        if self._decrypter is not None:
            if len(self._encryption_header) < 12:
                needed = 12 - len(self._encryption_header)
                self._encryption_header.extend(self._decrypter.decrypt(data[:needed]))
                start = needed
                if len(self._encryption_header) < 12:
                    return
                if self._encryption_header[-1] != self._encryption_check:
                    raise RuntimeError(f"Bad password for file {self._filename!r}")
            chunk_size = _DECRYPT_CHUNK_SIZE
        else:
            chunk_size = STREAM_CHUNK_SIZE
        for offset in range(start, len(data), chunk_size):
            chunk = data[offset : offset + chunk_size]
            if self._decrypter is not None:
                chunk = self._decrypter.decrypt(chunk)
            yield from self._decode_chunks(chunk)
            if self.eof:
                break

    def _decode_chunks(self, data: bytes) -> Iterator[bytes]:
        decoder = self._deflate if self._deflate is not None else self._incremental
        if decoder is None:
            yield self._check_output(data)
            self.eof = self._output_complete
            return
        # Reaching the output limit must not skip codec errors in the rest
        # of the compressed member. Drain in bounded chunks, discarding any
        # excess output in _check_output while retaining async checkpoints.
        while True:
            chunk = decoder.decompress(data, STREAM_CHUNK_SIZE)
            yield self._check_output(chunk)
            self.eof = decoder.eof
            if self.eof:
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
        """Verify CRC-32 even if the codec ended before the declared size.

        Like zipfile, compressed input exhaustion need not coincide with
        a codec end marker. However, input ending before the declared
        compressed size is an error unless the codec ended or the declared
        output size was already reached while decoding the received input.
        Only the bytes returned to the caller count toward the CRC.
        """
        if (
            self._strict_size
            and (self._deflate is not None or self._incremental is not None)
            and not self.eof
        ):
            raise TruncatedStream("Compressed ZIP entry is truncated")
        if self._compressed_left and not (self.eof or self._output_complete):
            raise TruncatedStream("Compressed ZIP entry is truncated")
        if self._decrypter is not None and len(self._encryption_header) != 12:
            raise BadZipFile("Truncated ZIP encryption header")
        self.eof = True
        actual_crc = self._crc & 0xFFFFFFFF
        if actual_crc != self._expected_crc:
            raise CRCMismatch(self._expected_crc, actual_crc)
        if (
            self._strict_size
            and self._expected_size is not None
            and self._output_size != self._expected_size
        ):
            raise SizeMismatch(self._expected_size, self._output_size)
