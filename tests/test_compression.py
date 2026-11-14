"""Round-trip tests against archives written by stdlib zipfile.

Every supported compression method is exercised with an empty, a
smallish, and a large (over 16 KiB) entry.  The archives are generated
with :mod:`zipfile` and read back through zipwire, so the parsers and
decompressors are tested against the exact bytes other tools write.

Regression test for #71: ZIP-LZMA payloads were handed to container
format decoders and every read of a ZIP-LZMA entry failed.
"""

from __future__ import annotations

import io
import random
import typing
import zipfile

import pytest

from tests.conftest import MockAsyncReader, MockSyncReader, make_zip
from zipwire import AsyncRemoteZip, SyncRemoteZip
from zipwire.backends import FileReader

if typing.TYPE_CHECKING:
    from pathlib import Path

# Deterministic, barely compressible content: compressed sizes stay in
# the same ballpark as the uncompressed ones, so the large entry also
# exceeds the 50 KiB prefetch threshold and exercises the streaming
# path of read_into().
ENTRIES: dict[str, bytes] = {
    "empty.bin": b"",
    "small.txt": b"Hello, compression World!\n" * 8,
    "large.bin": random.Random(42).randbytes(128 * 1024),
}

COMPRESSION_TYPES: list[pytest.ParameterSet] = [
    pytest.param(zipfile.ZIP_STORED, id="stored"),
    pytest.param(zipfile.ZIP_DEFLATED, id="deflated"),
    pytest.param(zipfile.ZIP_BZIP2, id="bzip2"),
    pytest.param(zipfile.ZIP_LZMA, id="lzma"),
]
if hasattr(zipfile, "ZIP_ZSTANDARD"):
    COMPRESSION_TYPES.append(pytest.param(zipfile.ZIP_ZSTANDARD, id="zstandard"))


@pytest.mark.parametrize("compression", COMPRESSION_TYPES)
def test_list_entries(compression: int) -> None:
    """The central directory is parsed for every compression method."""
    reader = MockSyncReader(make_zip(ENTRIES, compression=compression))
    with SyncRemoteZip(reader) as rz:
        assert rz.namelist() == list(ENTRIES)
        for info in rz.infolist():
            assert info.compress_type == compression
            assert info.file_size == len(ENTRIES[info.filename])


@pytest.mark.parametrize("compression", COMPRESSION_TYPES)
def test_read_entries(compression: int) -> None:
    """read() decodes empty, small, and large entries of every method."""
    reader = MockSyncReader(make_zip(ENTRIES, compression=compression))
    with SyncRemoteZip(reader) as rz:
        for name, expected in ENTRIES.items():
            assert rz.read(name) == expected


@pytest.mark.parametrize("compression", COMPRESSION_TYPES)
def test_read_into_entries(compression: int) -> None:
    """read_into() streams empty, small, and large entries of every method."""
    reader = MockSyncReader(make_zip(ENTRIES, compression=compression))
    with SyncRemoteZip(reader) as rz:
        for name, expected in ENTRIES.items():
            dest = io.BytesIO()
            rz.read_into(name, dest)
            assert dest.getvalue() == expected


@pytest.mark.parametrize("compression", COMPRESSION_TYPES)
async def test_async_read_entries(compression: int) -> None:
    """AsyncRemoteZip decodes entries of every compression method."""
    reader = MockAsyncReader(make_zip(ENTRIES, compression=compression))
    async with AsyncRemoteZip(reader) as rz:
        assert rz.namelist() == list(ENTRIES)
        for name, expected in ENTRIES.items():
            assert await rz.read(name) == expected
            dest = io.BytesIO()
            await rz.read_into(name, dest)
            assert dest.getvalue() == expected


def test_zip_lzma_file_reader(tmp_path: Path) -> None:
    """Regression test for #71: ZIP-LZMA read through a real file.

    Uses the FileReader backend exactly like the original report, where
    reading any ZIP-LZMA entry raised lzma.LZMAError.
    """
    path = tmp_path / "lzma.zip"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_LZMA) as zf:
        for name, content in ENTRIES.items():
            zf.writestr(name, content)

    with SyncRemoteZip(FileReader(path)) as rz:
        assert rz.namelist() == list(ENTRIES)
        for name, expected in ENTRIES.items():
            assert rz.read(name) == expected
            dest = io.BytesIO()
            rz.read_into(name, dest)
            assert dest.getvalue() == expected
