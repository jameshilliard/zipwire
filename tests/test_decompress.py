"""Tests for decompression and CRC verification."""

from __future__ import annotations

import bz2
import io
import zipfile
import zlib

import pytest

from tests.conftest import has_zstd, make_zip, needs_zstd
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
    sd = StreamingDecompressor(method=0, expected_crc=crc, dest=dest)
    sd.feed(data)
    sd.finish()
    assert dest.getvalue() == data


def test_streaming_stored_empty() -> None:
    data = b""
    crc = zlib.crc32(data) & 0xFFFFFFFF
    dest = io.BytesIO()
    sd = StreamingDecompressor(method=0, expected_crc=crc, dest=dest)
    sd.finish()
    assert dest.getvalue() == b""


def test_streaming_stored_binary() -> None:
    data = bytes(range(256))
    crc = zlib.crc32(data) & 0xFFFFFFFF
    dest = io.BytesIO()
    sd = StreamingDecompressor(method=0, expected_crc=crc, dest=dest)
    sd.feed(data)
    sd.finish()
    assert dest.getvalue() == data


def test_streaming_stored_chunked() -> None:
    data = b"A" * 1000
    crc = zlib.crc32(data) & 0xFFFFFFFF
    dest = io.BytesIO()
    sd = StreamingDecompressor(method=0, expected_crc=crc, dest=dest)
    for pos in range(0, len(data), 100):
        sd.feed(data[pos : pos + 100])
    sd.finish()
    assert dest.getvalue() == data


def test_streaming_deflated_simple() -> None:
    original = b"Hello, World!"
    compressed = zlib.compress(original, level=6)[2:-4]
    crc = zlib.crc32(original) & 0xFFFFFFFF
    dest = io.BytesIO()
    sd = StreamingDecompressor(method=8, expected_crc=crc, dest=dest)
    sd.feed(compressed)
    sd.finish()
    assert dest.getvalue() == original


def test_streaming_deflated_repeated() -> None:
    original = b"AAAA" * 1000
    compressed = zlib.compress(original, level=6)[2:-4]
    crc = zlib.crc32(original) & 0xFFFFFFFF
    dest = io.BytesIO()
    sd = StreamingDecompressor(method=8, expected_crc=crc, dest=dest)
    sd.feed(compressed)
    sd.finish()
    assert dest.getvalue() == original


def test_streaming_deflated_chunked() -> None:
    original = b"ABCDEFGH" * 500
    compressed = zlib.compress(original, level=6)[2:-4]
    crc = zlib.crc32(original) & 0xFFFFFFFF
    dest = io.BytesIO()
    sd = StreamingDecompressor(method=8, expected_crc=crc, dest=dest)
    for pos in range(0, len(compressed), 100):
        sd.feed(compressed[pos : pos + 100])
    sd.finish()
    assert dest.getvalue() == original


def test_streaming_crc_mismatch() -> None:
    data = b"Hello, World!"
    dest = io.BytesIO()
    sd = StreamingDecompressor(method=0, expected_crc=0xDEADBEEF, dest=dest)
    sd.feed(data)
    with pytest.raises(CRCMismatch):
        sd.finish()


def test_streaming_unsupported_method() -> None:
    dest = io.BytesIO()
    with pytest.raises(UnsupportedCompression, match="99"):
        StreamingDecompressor(method=99, expected_crc=0, dest=dest)


# --- bzip2 ---


def test_decompress_bzip2() -> None:
    original = b"Hello, bzip2 World!" * 50
    compressed = bz2.compress(original)
    crc = zlib.crc32(original) & 0xFFFFFFFF
    result = decompress(compressed, method=12, expected_size=len(original), expected_crc=crc)
    assert result == original


def test_streaming_bzip2() -> None:
    original = b"Streaming bzip2 data" * 100
    compressed = bz2.compress(original)
    crc = zlib.crc32(original) & 0xFFFFFFFF
    dest = io.BytesIO()
    sd = StreamingDecompressor(method=12, expected_crc=crc, dest=dest)
    sd.feed(compressed)
    sd.finish()
    assert dest.getvalue() == original


def test_streaming_bzip2_chunked() -> None:
    original = b"ABCDEFGH" * 500
    compressed = bz2.compress(original)
    crc = zlib.crc32(original) & 0xFFFFFFFF
    dest = io.BytesIO()
    sd = StreamingDecompressor(method=12, expected_crc=crc, dest=dest)
    for pos in range(0, len(compressed), 100):
        sd.feed(compressed[pos : pos + 100])
    sd.finish()
    assert dest.getvalue() == original


# --- lzma ---


def _lzma_payload(data: bytes) -> bytes:
    archive = make_zip({"file": data}, compression=zipfile.ZIP_LZMA)
    with zipfile.ZipFile(io.BytesIO(archive)) as zf:
        info = zf.getinfo("file")
    start = info.header_offset + 30 + len("file")
    return archive[start : start + info.compress_size]


def test_decompress_lzma() -> None:
    original = b"Hello, LZMA World!" * 50
    compressed = _lzma_payload(original)
    crc = zlib.crc32(original) & 0xFFFFFFFF
    result = decompress(compressed, method=14, expected_size=len(original), expected_crc=crc)
    assert result == original


def test_streaming_lzma() -> None:
    original = b"Streaming LZMA data" * 100
    compressed = _lzma_payload(original)
    crc = zlib.crc32(original) & 0xFFFFFFFF
    dest = io.BytesIO()
    sd = StreamingDecompressor(method=14, expected_crc=crc, dest=dest)
    sd.feed(compressed)
    sd.finish()
    assert dest.getvalue() == original


def test_streaming_lzma_chunked() -> None:
    original = b"ABCDEFGH" * 500
    compressed = _lzma_payload(original)
    crc = zlib.crc32(original) & 0xFFFFFFFF
    dest = io.BytesIO()
    sd = StreamingDecompressor(method=14, expected_crc=crc, dest=dest)
    for pos in range(0, len(compressed), 100):
        sd.feed(compressed[pos : pos + 100])
    sd.finish()
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
    sd = StreamingDecompressor(method=93, expected_crc=crc, dest=dest)
    sd.feed(compressed)
    sd.finish()
    assert dest.getvalue() == original


@needs_zstd
def test_streaming_zstandard_chunked() -> None:
    original = b"ABCDEFGH" * 500
    compressed = zstd.compress(original)
    crc = zlib.crc32(original) & 0xFFFFFFFF
    dest = io.BytesIO()
    sd = StreamingDecompressor(method=93, expected_crc=crc, dest=dest)
    for pos in range(0, len(compressed), 100):
        sd.feed(compressed[pos : pos + 100])
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
    sd = StreamingDecompressor(method=93, expected_crc=0xDEADBEEF, dest=dest)
    sd.feed(compressed)
    with pytest.raises(CRCMismatch):
        sd.finish()


# --- FileTooLarge ---


def test_streaming_file_too_large() -> None:
    data = b"A" * 100
    crc = zlib.crc32(data) & 0xFFFFFFFF
    dest = io.BytesIO()
    sd = StreamingDecompressor(method=0, expected_crc=crc, dest=dest, max_output_size=10)
    with pytest.raises(FileTooLarge):
        sd.feed(data)
