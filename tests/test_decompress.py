"""Tests for decompression and CRC verification."""

from __future__ import annotations

import io
import zlib

import pytest

from tests.conftest import has_zstd, needs_zstd
from zipwire._decompress import StreamingDecompressor, decompress
from zipwire._errors import CRCMismatch, FileTooLarge, UnsupportedCompression

if has_zstd:
    from compression import zstd

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
    dest.write(sd.finish())
    assert dest.getvalue() == data


def test_streaming_stored_empty() -> None:
    data = b""
    crc = zlib.crc32(data) & 0xFFFFFFFF
    dest = io.BytesIO()
    sd = StreamingDecompressor(method=0, expected_crc=crc)
    dest.write(sd.finish())
    assert dest.getvalue() == b""


def test_streaming_stored_binary() -> None:
    data = bytes(range(256))
    crc = zlib.crc32(data) & 0xFFFFFFFF
    dest = io.BytesIO()
    sd = StreamingDecompressor(method=0, expected_crc=crc)
    dest.write(sd.feed(data))
    dest.write(sd.finish())
    assert dest.getvalue() == data


def test_streaming_stored_chunked() -> None:
    data = b"A" * 1000
    crc = zlib.crc32(data) & 0xFFFFFFFF
    dest = io.BytesIO()
    sd = StreamingDecompressor(method=0, expected_crc=crc)
    for pos in range(0, len(data), 100):
        dest.write(sd.feed(data[pos : pos + 100]))
    dest.write(sd.finish())
    assert dest.getvalue() == data


def test_streaming_deflated_simple() -> None:
    original = b"Hello, World!"
    compressed = zlib.compress(original, level=6)[2:-4]
    crc = zlib.crc32(original) & 0xFFFFFFFF
    dest = io.BytesIO()
    sd = StreamingDecompressor(method=8, expected_crc=crc)
    dest.write(sd.feed(compressed))
    dest.write(sd.finish())
    assert dest.getvalue() == original


def test_streaming_deflated_repeated() -> None:
    original = b"AAAA" * 1000
    compressed = zlib.compress(original, level=6)[2:-4]
    crc = zlib.crc32(original) & 0xFFFFFFFF
    dest = io.BytesIO()
    sd = StreamingDecompressor(method=8, expected_crc=crc)
    dest.write(sd.feed(compressed))
    dest.write(sd.finish())
    assert dest.getvalue() == original


def test_streaming_deflated_chunked() -> None:
    original = b"ABCDEFGH" * 500
    compressed = zlib.compress(original, level=6)[2:-4]
    crc = zlib.crc32(original) & 0xFFFFFFFF
    dest = io.BytesIO()
    sd = StreamingDecompressor(method=8, expected_crc=crc)
    for pos in range(0, len(compressed), 100):
        dest.write(sd.feed(compressed[pos : pos + 100]))
    dest.write(sd.finish())
    assert dest.getvalue() == original


def test_streaming_crc_mismatch() -> None:
    data = b"Hello, World!"
    dest = io.BytesIO()
    sd = StreamingDecompressor(method=0, expected_crc=0xDEADBEEF)
    dest.write(sd.feed(data))
    with pytest.raises(CRCMismatch):
        dest.write(sd.finish())


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
    dest.write(sd.finish())
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
    dest.write(sd.finish())
    assert dest.getvalue() == original


# --- lzma ---


def test_decompress_lzma() -> None:
    import lzma

    original = b"Hello, LZMA World!" * 50
    compressed = lzma.compress(original)
    crc = zlib.crc32(original) & 0xFFFFFFFF
    result = decompress(compressed, method=14, expected_size=len(original), expected_crc=crc)
    assert result == original


def test_streaming_lzma() -> None:
    import lzma

    original = b"Streaming LZMA data" * 100
    compressed = lzma.compress(original)
    crc = zlib.crc32(original) & 0xFFFFFFFF
    dest = io.BytesIO()
    sd = StreamingDecompressor(method=14, expected_crc=crc)
    dest.write(sd.feed(compressed))
    dest.write(sd.finish())
    assert dest.getvalue() == original


def test_streaming_lzma_chunked() -> None:
    import lzma

    original = b"ABCDEFGH" * 500
    compressed = lzma.compress(original)
    crc = zlib.crc32(original) & 0xFFFFFFFF
    dest = io.BytesIO()
    sd = StreamingDecompressor(method=14, expected_crc=crc)
    for pos in range(0, len(compressed), 100):
        dest.write(sd.feed(compressed[pos : pos + 100]))
    dest.write(sd.finish())
    assert dest.getvalue() == original


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
    dest.write(sd.finish())
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
    dest.write(sd.finish())
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
        dest.write(sd.finish())


# --- FileTooLarge ---


def test_streaming_file_too_large() -> None:
    data = b"A" * 100
    crc = zlib.crc32(data) & 0xFFFFFFFF
    dest = io.BytesIO()
    sd = StreamingDecompressor(method=0, expected_crc=crc, max_output_size=10)
    with pytest.raises(FileTooLarge):
        dest.write(sd.feed(data))
