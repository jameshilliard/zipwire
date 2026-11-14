"""Tests for decompression and CRC verification."""

from __future__ import annotations

import asyncio
import bz2
import io
import lzma
import typing
import zlib

import pytest

from tests.conftest import has_zstd, needs_zstd
from zipwire._constants import STREAM_CHUNK_SIZE
from zipwire._decompress import StreamingDecompressor, adecompress, decompress
from zipwire._errors import (
    CRCMismatch,
    FileTooLarge,
    SizeMismatch,
    TruncatedStream,
    UnsupportedCompression,
)

if typing.TYPE_CHECKING:
    from collections.abc import Callable

if has_zstd:
    from compression import zstd

if has_zstd:

    def _zstd_compress(data: bytes) -> bytes:
        return zstd.compress(data)

else:

    def _zstd_compress(data: bytes) -> bytes:  # pragma: no cover
        raise AssertionError("zstandard is not available")

# --- decompress() ---


def test_decompress_stored_simple() -> None:
    data = b"Hello, World!"
    crc = zlib.crc32(data) & 0xFFFFFFFF
    result = decompress(data, method=0, expected_size=len(data), expected_crc=crc)
    assert result == data


def test_decompress_stored_empty() -> None:
    data = b""
    crc = zlib.crc32(data) & 0xFFFFFFFF
    result = decompress(data, method=0, expected_size=0, expected_crc=crc)
    assert result == b""


def test_decompress_stored_binary() -> None:
    data = bytes(range(256))
    crc = zlib.crc32(data) & 0xFFFFFFFF
    result = decompress(data, method=0, expected_size=len(data), expected_crc=crc)
    assert result == data


def test_decompress_deflated_simple() -> None:
    original = b"Hello, World!"
    compressed = zlib.compress(original, level=6)[2:-4]  # strip zlib header/trailer
    crc = zlib.crc32(original) & 0xFFFFFFFF
    result = decompress(compressed, method=8, expected_size=len(original), expected_crc=crc)
    assert result == original


def test_decompress_deflated_repeated() -> None:
    original = b"AAAA" * 1000
    compressed = zlib.compress(original, level=6)[2:-4]
    crc = zlib.crc32(original) & 0xFFFFFFFF
    result = decompress(compressed, method=8, expected_size=len(original), expected_crc=crc)
    assert result == original


def test_decompress_crc_mismatch() -> None:
    data = b"Hello, World!"
    with pytest.raises(CRCMismatch):
        decompress(data, method=0, expected_size=len(data), expected_crc=0xDEADBEEF)


def test_decompress_unsupported_method() -> None:
    with pytest.raises(UnsupportedCompression, match="99"):
        decompress(b"data", method=99, expected_size=4, expected_crc=0)


# --- StreamingDecompressor ---


def test_streaming_stored_simple() -> None:
    data = b"Hello, World!"
    crc = zlib.crc32(data) & 0xFFFFFFFF
    dest = io.BytesIO()
    sd = StreamingDecompressor(method=0, expected_crc=crc)
    dest.write(sd.feed(data))
    sd.finish()
    assert dest.getvalue() == data


def test_streaming_stored_empty() -> None:
    data = b""
    crc = zlib.crc32(data) & 0xFFFFFFFF
    dest = io.BytesIO()
    sd = StreamingDecompressor(method=0, expected_crc=crc)
    sd.finish()
    assert dest.getvalue() == b""


def test_streaming_stored_binary() -> None:
    data = bytes(range(256))
    crc = zlib.crc32(data) & 0xFFFFFFFF
    dest = io.BytesIO()
    sd = StreamingDecompressor(method=0, expected_crc=crc)
    dest.write(sd.feed(data))
    sd.finish()
    assert dest.getvalue() == data


def test_streaming_stored_chunked() -> None:
    data = b"A" * 1000
    crc = zlib.crc32(data) & 0xFFFFFFFF
    dest = io.BytesIO()
    sd = StreamingDecompressor(method=0, expected_crc=crc)
    for pos in range(0, len(data), 100):
        dest.write(sd.feed(data[pos : pos + 100]))
    sd.finish()
    assert dest.getvalue() == data


def test_streaming_deflated_simple() -> None:
    original = b"Hello, World!"
    compressed = zlib.compress(original, level=6)[2:-4]
    crc = zlib.crc32(original) & 0xFFFFFFFF
    dest = io.BytesIO()
    sd = StreamingDecompressor(method=8, expected_crc=crc)
    dest.write(sd.feed(compressed))
    sd.finish()
    assert dest.getvalue() == original


def test_streaming_deflated_repeated() -> None:
    original = b"AAAA" * 1000
    compressed = zlib.compress(original, level=6)[2:-4]
    crc = zlib.crc32(original) & 0xFFFFFFFF
    dest = io.BytesIO()
    sd = StreamingDecompressor(method=8, expected_crc=crc)
    dest.write(sd.feed(compressed))
    sd.finish()
    assert dest.getvalue() == original


def test_streaming_deflated_chunked() -> None:
    original = b"ABCDEFGH" * 500
    compressed = zlib.compress(original, level=6)[2:-4]
    crc = zlib.crc32(original) & 0xFFFFFFFF
    dest = io.BytesIO()
    sd = StreamingDecompressor(method=8, expected_crc=crc)
    for pos in range(0, len(compressed), 100):
        dest.write(sd.feed(compressed[pos : pos + 100]))
    sd.finish()
    assert dest.getvalue() == original


def test_streaming_crc_mismatch() -> None:
    data = b"Hello, World!"
    dest = io.BytesIO()
    sd = StreamingDecompressor(method=0, expected_crc=0xDEADBEEF)
    dest.write(sd.feed(data))
    with pytest.raises(CRCMismatch):
        sd.finish()


def test_streaming_unsupported_method() -> None:
    with pytest.raises(UnsupportedCompression, match="99"):
        StreamingDecompressor(method=99, expected_crc=0)


# --- bzip2 ---


def test_decompress_bzip2() -> None:
    import bz2

    original = b"Hello, bzip2 World!" * 50
    compressed = bz2.compress(original)
    crc = zlib.crc32(original) & 0xFFFFFFFF
    result = decompress(compressed, method=12, expected_size=len(original), expected_crc=crc)
    assert result == original


def test_streaming_bzip2() -> None:
    import bz2

    original = b"Streaming bzip2 data" * 100
    compressed = bz2.compress(original)
    crc = zlib.crc32(original) & 0xFFFFFFFF
    dest = io.BytesIO()
    sd = StreamingDecompressor(method=12, expected_crc=crc)
    dest.write(sd.feed(compressed))
    sd.finish()
    assert dest.getvalue() == original


def test_streaming_bzip2_chunked() -> None:
    import bz2

    original = b"ABCDEFGH" * 500
    compressed = bz2.compress(original)
    crc = zlib.crc32(original) & 0xFFFFFFFF
    dest = io.BytesIO()
    sd = StreamingDecompressor(method=12, expected_crc=crc)
    for pos in range(0, len(compressed), 100):
        dest.write(sd.feed(compressed[pos : pos + 100]))
    sd.finish()
    assert dest.getvalue() == original


# --- lzma ---


def zip_lzma_compress(data: bytes) -> bytes:
    """Compress *data* as a ZIP-LZMA (method 14) payload.

    A ZIP-LZMA payload is not an xz or .lzma container: it starts with
    two version bytes, a two-byte little-endian property size, five
    bytes of raw LZMA1 properties (lc=3, lp=0, pb=2, 8 MiB dictionary),
    and then the raw LZMA1 stream with an end-of-stream marker.  This
    mirrors what :mod:`zipfile` writes for ``ZIP_LZMA``.
    """
    filters = [{"id": lzma.FILTER_LZMA1, "lc": 3, "lp": 0, "pb": 2, "dict_size": 8 << 20}]
    props = bytes([2 * 45 + 0 * 9 + 3]) + (8 << 20).to_bytes(4, "little")
    compressor = lzma.LZMACompressor(lzma.FORMAT_RAW, filters=filters)
    header = bytes((9, 4)) + len(props).to_bytes(2, "little") + props
    return header + compressor.compress(data) + compressor.flush()


def test_decompress_lzma() -> None:
    original = b"Hello, LZMA World!" * 50
    compressed = zip_lzma_compress(original)
    crc = zlib.crc32(original) & 0xFFFFFFFF
    result = decompress(compressed, method=14, expected_size=len(original), expected_crc=crc)
    assert result == original


def test_decompress_lzma_empty() -> None:
    compressed = zip_lzma_compress(b"")
    crc = zlib.crc32(b"") & 0xFFFFFFFF
    result = decompress(compressed, method=14, expected_size=0, expected_crc=crc)
    assert result == b""


def test_decompress_lzma_bad_header() -> None:
    """Malformed ZIP-LZMA headers are rejected instead of decoded."""
    # Payload shorter than the four fixed header bytes.
    with pytest.raises(lzma.LZMAError):
        decompress(b"\x09\x04", method=14, expected_size=0, expected_crc=0)
    # Property size that the payload never satisfies.
    with pytest.raises(lzma.LZMAError):
        decompress(b"\x09\x04\xff\xff", method=14, expected_size=0, expected_crc=0)
    # Property size other than the five bytes LZMA1 properties use.
    bad_size = b"\x09\x04\x04\x00\x5d\x00\x00\x80\x00"
    with pytest.raises(lzma.LZMAError):
        decompress(bad_size, method=14, expected_size=0, expected_crc=0)
    # Properties byte with an out-of-range lc/lp/pb encoding.
    bad_props = b"\x09\x04\x05\x00\xe1\x00\x00\x80\x00"
    with pytest.raises(lzma.LZMAError):
        decompress(bad_props, method=14, expected_size=0, expected_crc=0)


def test_decompress_lzma_truncated() -> None:
    """A one-shot decode of a stream missing its end marker is rejected."""
    original = b"Hello, LZMA World!" * 50
    compressed = zip_lzma_compress(original)
    with pytest.raises(lzma.LZMAError):
        decompress(compressed[:-6], method=14, expected_size=len(original), expected_crc=0)


def test_decompress_lzma_oversized_dict_clamped() -> None:
    """An oversized dictionary claim is clamped instead of trusted.

    The properties come from the remote archive.  A hostile entry can
    claim a multi-gigabyte dictionary; decoding clamps it to
    ZipLZMADecompressor._MAX_DICT_SIZE, which does not affect the
    decoded bytes because a match never references farther back than
    the output decoded so far.
    """
    original = b"Hello, LZMA World!" * 50
    crc = zlib.crc32(original) & 0xFFFFFFFF
    compressed = bytearray(zip_lzma_compress(original))
    # Patch the dictionary size inside the properties (the last four
    # bytes of the five-byte properties that follow the four header
    # bytes) to claim 4 GiB - 1.
    compressed[5:9] = (0xFFFFFFFF).to_bytes(4, "little")
    result = decompress(
        bytes(compressed), method=14, expected_size=len(original), expected_crc=crc
    )
    assert result == original


def test_streaming_lzma() -> None:
    original = b"Streaming LZMA data" * 100
    compressed = zip_lzma_compress(original)
    crc = zlib.crc32(original) & 0xFFFFFFFF
    dest = io.BytesIO()
    sd = StreamingDecompressor(method=14, expected_crc=crc)
    dest.write(sd.feed(compressed))
    sd.finish()
    assert dest.getvalue() == original


def test_streaming_lzma_chunked() -> None:
    """Feeds smaller than the ZIP-LZMA header exercise its buffering."""
    original = b"ABCDEFGH" * 500
    compressed = zip_lzma_compress(original)
    crc = zlib.crc32(original) & 0xFFFFFFFF
    dest = io.BytesIO()
    sd = StreamingDecompressor(method=14, expected_crc=crc)
    for pos in range(0, len(compressed), 100):
        dest.write(sd.feed(compressed[pos : pos + 100]))
    sd.finish()
    assert dest.getvalue() == original


def test_streaming_lzma_truncated() -> None:
    """A stream cut inside the end-of-stream marker is rejected.

    The partial output already matches the expected size and CRC, so
    only the end-of-stream check catches the truncation.
    """
    original = b"Hello, LZMA World!" * 50
    compressed = zip_lzma_compress(original)
    crc = zlib.crc32(original) & 0xFFFFFFFF
    sd = StreamingDecompressor(method=14, expected_crc=crc, expected_size=len(original))
    sd.feed(compressed[:-6])
    with pytest.raises(TruncatedStream):
        sd.finish()


def test_streaming_lzma_header_truncated() -> None:
    """A stream that ends inside the ZIP-LZMA header is rejected."""
    sd = StreamingDecompressor(method=14, expected_crc=0)
    sd.feed(b"\x09\x04\x05")
    with pytest.raises(TruncatedStream):
        sd.finish()


# --- zstandard ---


@needs_zstd
def test_decompress_zstandard() -> None:
    original = b"Hello, Zstandard World!" * 50
    compressed = zstd.compress(original)
    crc = zlib.crc32(original) & 0xFFFFFFFF
    result = decompress(compressed, method=93, expected_size=len(original), expected_crc=crc)
    assert result == original


@needs_zstd
def test_streaming_zstandard() -> None:
    original = b"Streaming Zstandard data" * 100
    compressed = zstd.compress(original)
    crc = zlib.crc32(original) & 0xFFFFFFFF
    dest = io.BytesIO()
    sd = StreamingDecompressor(method=93, expected_crc=crc)
    dest.write(sd.feed(compressed))
    sd.finish()
    assert dest.getvalue() == original


@needs_zstd
def test_streaming_zstandard_chunked() -> None:
    original = b"ABCDEFGH" * 500
    compressed = zstd.compress(original)
    crc = zlib.crc32(original) & 0xFFFFFFFF
    dest = io.BytesIO()
    sd = StreamingDecompressor(method=93, expected_crc=crc)
    for pos in range(0, len(compressed), 100):
        dest.write(sd.feed(compressed[pos : pos + 100]))
    sd.finish()
    assert dest.getvalue() == original


@needs_zstd
def test_decompress_zstandard_binary() -> None:
    original = bytes(range(256))
    compressed = zstd.compress(original)
    crc = zlib.crc32(original) & 0xFFFFFFFF
    result = decompress(compressed, method=93, expected_size=len(original), expected_crc=crc)
    assert result == original


@needs_zstd
def test_decompress_zstandard_crc_mismatch() -> None:
    original = b"Hello, Zstandard World!"
    compressed = zstd.compress(original)
    with pytest.raises(CRCMismatch):
        decompress(compressed, method=93, expected_size=len(original), expected_crc=0xDEADBEEF)


@needs_zstd
def test_streaming_zstandard_crc_mismatch() -> None:
    original = b"Hello, Zstandard World!"
    compressed = zstd.compress(original)
    dest = io.BytesIO()
    sd = StreamingDecompressor(method=93, expected_crc=0xDEADBEEF)
    dest.write(sd.feed(compressed))
    with pytest.raises(CRCMismatch):
        sd.finish()


# --- FileTooLarge ---


def test_streaming_file_too_large() -> None:
    data = b"A" * 100
    crc = zlib.crc32(data) & 0xFFFFFFFF
    dest = io.BytesIO()
    sd = StreamingDecompressor(method=0, expected_crc=crc, max_output_size=10)
    with pytest.raises(FileTooLarge):
        dest.write(sd.feed(data))


# --- bounded streaming output ---


def _deflate_compress(data: bytes) -> bytes:
    return zlib.compress(data, level=6)[2:-4]


@pytest.mark.parametrize(
    ("method", "compress"),
    [
        (8, _deflate_compress),
        (12, bz2.compress),
        (14, zip_lzma_compress),
        pytest.param(93, _zstd_compress, marks=needs_zstd),
    ],
    ids=["deflated", "bzip2", "lzma", "zstandard"],
)
def test_feed_chunks_are_bounded(method: int, compress: Callable[[bytes], bytes]) -> None:
    """A highly compressible entry cannot inflate into one huge chunk."""
    original = b"\0" * (2 * STREAM_CHUNK_SIZE + 123)
    compressed = compress(original)
    crc = zlib.crc32(original) & 0xFFFFFFFF
    sd = StreamingDecompressor(method, expected_crc=crc)
    chunks = list(sd.feed_chunks(compressed))
    assert max(map(len, chunks)) <= STREAM_CHUNK_SIZE
    assert b"".join(chunks) == original
    assert sd.finish() is None  # type: ignore[func-returns-value]


def test_stored_feed_chunks_are_bounded() -> None:
    original = b"x" * (2 * STREAM_CHUNK_SIZE + 1)
    crc = zlib.crc32(original) & 0xFFFFFFFF
    sd = StreamingDecompressor(method=0, expected_crc=crc)
    chunks = list(sd.feed_chunks(original))
    assert [len(chunk) for chunk in chunks] == [STREAM_CHUNK_SIZE, STREAM_CHUNK_SIZE, 1]
    assert sd.finish() is None  # type: ignore[func-returns-value]


def test_feed_chunks_yields_empty_step() -> None:
    """Steps without output are still yielded as scheduling checkpoints."""
    original = b"checkpoint" * 100
    compressed = bz2.compress(original)
    crc = zlib.crc32(original) & 0xFFFFFFFF
    sd = StreamingDecompressor(method=12, expected_crc=crc)
    assert list(sd.feed_chunks(compressed[:3])) == [b""]
    assert sd.feed(compressed[3:]) == original
    assert sd.finish() is None  # type: ignore[func-returns-value]


async def test_adecompress_stored() -> None:
    data = bytes(range(256)) * 100
    crc = zlib.crc32(data) & 0xFFFFFFFF
    assert await adecompress(data, method=0, expected_size=len(data), expected_crc=crc) == data
    with pytest.raises(CRCMismatch):
        await adecompress(data, method=0, expected_size=len(data), expected_crc=0xDEADBEEF)


async def test_adecompress_yields_to_event_loop() -> None:
    """Decoding a large entry is split so other tasks can run."""
    original = b"\0" * (2 * STREAM_CHUNK_SIZE)
    compressed = _deflate_compress(original)
    crc = zlib.crc32(original) & 0xFFFFFFFF
    observed = False

    async def observer() -> None:
        nonlocal observed
        observed = True

    task = asyncio.create_task(observer())
    try:
        result = await adecompress(
            compressed, method=8, expected_size=len(original), expected_crc=crc
        )
        assert result == original
        assert observed
    finally:
        await task


# --- truncated streams and size mismatches ---


def test_decompress_size_mismatch() -> None:
    data = b"Hello, World!"
    crc = zlib.crc32(data) & 0xFFFFFFFF
    with pytest.raises(SizeMismatch):
        decompress(data, method=0, expected_size=len(data) - 1, expected_crc=crc)


def test_stored_feed_returns_input_buffer() -> None:
    """STORED feed() hands the buffer through instead of copying it."""
    data = b"A" * (STREAM_CHUNK_SIZE + 1)
    crc = zlib.crc32(data) & 0xFFFFFFFF
    sd = StreamingDecompressor(method=0, expected_crc=crc)
    assert sd.feed(data) is data


def test_streaming_expected_size() -> None:
    original = b"Hello, World!" * 10
    compressed = _deflate_compress(original)
    crc = zlib.crc32(original) & 0xFFFFFFFF
    sd = StreamingDecompressor(method=8, expected_crc=crc, expected_size=len(original))
    sd.feed(compressed)
    assert sd.finish() is None  # type: ignore[func-returns-value]


def test_streaming_expected_size_mismatch() -> None:
    original = b"Hello, World!" * 10
    compressed = _deflate_compress(original)
    crc = zlib.crc32(original) & 0xFFFFFFFF
    sd = StreamingDecompressor(method=8, expected_crc=crc, expected_size=len(original) + 1)
    sd.feed(compressed)
    with pytest.raises(SizeMismatch):
        sd.finish()


@pytest.mark.parametrize("method", [0, 8], ids=["stored", "deflated"])
def test_streaming_expected_size_exceeded_during_feed(method: int) -> None:
    """Output beyond the expected size is rejected while it is produced."""
    original = b"Hello, World!" * 100
    if method == 0:
        payload: bytes = original
    else:
        payload = _deflate_compress(original)
    crc = zlib.crc32(original) & 0xFFFFFFFF
    sd = StreamingDecompressor(method=method, expected_crc=crc, expected_size=10)
    with pytest.raises(SizeMismatch):
        sd.feed(payload)


def test_streaming_finish_truncated() -> None:
    """A compressed stream that never reaches its end is rejected."""
    original = b"Hello, World!" * 10
    compressed = _deflate_compress(original)
    crc = zlib.crc32(original) & 0xFFFFFFFF
    sd = StreamingDecompressor(method=8, expected_crc=crc)
    sd.feed(compressed[: len(compressed) // 2])
    with pytest.raises(TruncatedStream, match="truncated"):
        sd.finish()


@pytest.mark.parametrize(
    ("method", "compress"),
    [
        (8, _deflate_compress),
        (12, bz2.compress),
        (14, zip_lzma_compress),
        pytest.param(93, _zstd_compress, marks=needs_zstd),
    ],
    ids=["deflated", "bzip2", "lzma", "zstandard"],
)
def test_streaming_feed_after_end_raises(method: int, compress: Callable[[bytes], bytes]) -> None:
    """Input past the end of the stream is rejected instead of dropped."""
    original = b"trailing data test" * 100
    compressed = compress(original)
    crc = zlib.crc32(original) & 0xFFFFFFFF
    sd = StreamingDecompressor(method, expected_crc=crc)
    sd.feed(compressed)
    assert sd.eof
    with pytest.raises(TruncatedStream, match="after end"):
        sd.feed(b"trailing garbage")
    # An empty feed after the end stays harmless.
    assert list(sd.feed_chunks(b"")) == []


async def test_adecompress_truncated() -> None:
    """A truncated stream with a CRC matching the partial output is rejected.

    Without the end-of-stream check, ``adecompress()`` returned the
    partial output for this input.
    """
    original = b"Hello, World!" * 10
    compressed = _deflate_compress(original)
    part = compressed[: len(compressed) // 2]
    partial = zlib.decompressobj(-15).decompress(part)
    crc = zlib.crc32(partial) & 0xFFFFFFFF
    with pytest.raises(TruncatedStream):
        await adecompress(part, method=8, expected_size=len(partial), expected_crc=crc)


async def test_adecompress_size_mismatch() -> None:
    data = b"Hello, World!"
    crc = zlib.crc32(data) & 0xFFFFFFFF
    with pytest.raises(SizeMismatch):
        await adecompress(data, method=0, expected_size=len(data) + 5, expected_crc=crc)
