"""Archive-reading compatibility with stdlib zipfile, including streaming."""

from __future__ import annotations

import base64
import io
import struct
import subprocess
import sys
import typing
import warnings
import zipfile
import zlib
from contextlib import AsyncExitStack, asynccontextmanager
from random import Random

import pytest

from tests.conftest import MockAsyncReader, MockSyncReader, make_zip, needs_zstd
from zipwire import AsyncRemoteWheel, AsyncRemoteZip, SyncRemoteWheel, SyncRemoteZip
from zipwire import _decompress as decoding
from zipwire._constants import (
    EOCD_STRUCT,
    STREAM_CHUNK_SIZE,
    ZIP64_EOCD_LOCATOR_STRUCT,
    ZIP64_EOCD_STRUCT,
)
from zipwire._decompress import StreamingDecompressor
from zipwire.backends import AsyncFileReader, FileReader

if typing.TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Iterator
    from pathlib import Path

Archive = SyncRemoteZip | AsyncRemoteZip
_WHEEL_URL = "https://example.com/demo-1.0-py3-none-any.whl"
_ENCRYPTED_CONTENT = b"encrypted payload\n" * 20

# Fixed reference archives made by 7-Zip (stored/LZMA) and Info-ZIP
# (Deflate with a data descriptor), using password b"test". Keeping the
# archives here avoids dependencies on external tools in the test suite.
_ENCRYPTED = {
    "stored": (
        "UEsDBBQDAQAAALGVSl2yyIACdAEAAGgBAAAIAAAAZmlsZS50eHQ4KBI0wOK7MDuvZ8C3GtAKIShpLoDh22/s2gBc"
        "htvSfP75uDJd1qoeVekCAc7d2DkECtymfhlI+hRAgUdhIB6eAxAr2xOnSFPiFyKwBK+eLLFrmIzHIDgwjhxKPl1i"
        "Gj+mT/oRHkcAYUN9KRNBKyhA3Eh0S2mznwBmYD/pYxkgeRNJtJ53L+/v7SOmbk7npMuT0VTCokmnIFo/vhWVVJ3g"
        "FhjQzrEM53nsaMt8J35C8+9wdngqgMh1ZsdVbqoCAOoBsotTPUaGdvN2i6cOw5OUgoB2ixwx4ibwTq3I60dxv7pr"
        "LWM01dymE31998MBDK3Fb4yrx4DyfBtgFwT0KmLhCyoXddmqBVZ1T+OcubHYTqCVHXgTdfMZFPnYQQ8ycN0jrsoS"
        "cNXTqheRywyaX+whw5AK2PBTluU36/fDvayPaq04yECDXg507hWAxT8KnTJ1lNfUS4BFiEkkTJwUAExGVKMtscwd"
        "5PmHR9sHYtT+qZvNNB1QSwECPwMUAwEAAACxlUpdssiAAnQBAABoAQAACAAkAAAAAAAAACCApIEAAAAAZmlsZS50"
        "eHQKACAAAAAAAAEAGAAAi//SGVndAQCL/9IZWd0BAIv/0hlZ3QFQSwUGAAAAAAEAAQBaAAAAmgEAAAAA"
    ),
    "lzma": (
        "UEsDBD8DAwAOALGVSl2yyIACNgAAAGgBAAAIAAAAZmlsZS50eHQEma4InWiQrG9lQmaNgRCPdLZdibt6s3ZhY8ez"
        "mxYHyE2cjB41Bit8jLDP3jT+Q/vWz2Ae/rpQSwECPwM/AwMADgCxlUpdssiAAjYAAABoAQAACAAkAAAAAAAAACCA"
        "pIEAAAAAZmlsZS50eHQKACAAAAAAAAEAGAAAi//SGVndAQCL/9IZWd0BAIv/0hlZ3QFQSwUGAAAAAAEAAQBaAAAA"
        "XAAAAAAA"
    ),
    "descriptor": (
        "UEsDBBQACQAIALGVSl2yyIACJQAAAGgBAAAIABwAZmlsZS50eHRVVAkAAy7cymou3MpqdXgLAAEE9QEAAAQUAAAA"
        "4SnmSXm+qM1vrL1JWRHOiDwcbmWK4cB/NEeJVtVmE8fgn88wO1BLBwiyyIACJQAAAGgBAABQSwECHgMUAAkACACx"
        "lUpdssiAAiUAAABoAQAACAAYAAAAAAABAAAApIEAAAAAZmlsZS50eHRVVAUAAy7cymp1eAsAAQT1AQAABBQAAABQ"
        "SwUGAAAAAAEAAQBOAAAAdwAAAAAA"
    ),
}


@pytest.fixture(params=["sync", "async", "sync_wheel", "async_wheel"])
def kind(request: pytest.FixtureRequest) -> str:
    return request.param


@asynccontextmanager
async def opened(
    data: bytes, kind: str, *, metadata_encoding: str | None = None
) -> AsyncGenerator[Archive, None]:
    if kind.startswith("async"):
        cls = AsyncRemoteWheel if kind.endswith("wheel") else AsyncRemoteZip
        reader = MockAsyncReader(data, url=_WHEEL_URL)
        try:
            async with cls(reader, metadata_encoding=metadata_encoding) as archive:
                yield archive
        finally:
            assert reader.closed
    else:
        cls = SyncRemoteWheel if kind.endswith("wheel") else SyncRemoteZip
        reader = MockSyncReader(data, url=_WHEEL_URL)
        try:
            with cls(reader, metadata_encoding=metadata_encoding) as archive:
                yield archive
        finally:
            assert reader.closed


async def read(
    archive: Archive,
    name: str | zipfile.ZipInfo,
    *,
    into: bool = False,
    pwd: bytes | None = None,
    max_file_size: int | None = None,
) -> bytes:
    if into:
        dest = io.BytesIO()
        if isinstance(archive, AsyncRemoteZip):
            await archive.read_into(name, dest, pwd=pwd, max_file_size=max_file_size)
        else:
            archive.read_into(name, dest, pwd=pwd, max_file_size=max_file_size)
        return dest.getvalue()
    if isinstance(archive, AsyncRemoteZip):
        return await archive.read(name, pwd=pwd, max_file_size=max_file_size)
    return archive.read(name, pwd=pwd, max_file_size=max_file_size)


def zip64(data: bytes, *, comment: bytes = b"", extension: bytes = b"") -> bytes:
    fields = EOCD_STRUCT.unpack_from(data, len(data) - EOCD_STRUCT.size)
    prefix = data[: -EOCD_STRUCT.size]
    record = ZIP64_EOCD_STRUCT.pack(
        b"PK\x06\x06",
        44 + len(extension),
        45,
        45,
        0,
        0,
        fields[3],
        fields[4],
        fields[5],
        fields[6],
    )
    locator = ZIP64_EOCD_LOCATOR_STRUCT.pack(b"PK\x06\x07", 0, len(prefix), 1)
    eocd = EOCD_STRUCT.pack(
        b"PK\x05\x06", 0, 0, 0xFFFF, 0xFFFF, 0xFFFFFFFF, 0xFFFFFFFF, len(comment)
    )
    return prefix + record + extension + locator + eocd + comment


class Unseekable(io.BytesIO):
    def seek(self, *args: object) -> int:
        raise io.UnsupportedOperation("not seekable")


@pytest.mark.parametrize(
    "method", [zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED, zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA]
)
@pytest.mark.parametrize("size", [0, 100, 70000])
async def test_compression(kind: str, method: int, size: int) -> None:
    payload = Random(0).randbytes(size)
    data = make_zip({"file": payload}, compression=method)
    with zipfile.ZipFile(io.BytesIO(data)) as reference:
        expected = reference.read("file")
    async with opened(data, kind) as archive:
        assert await read(archive, "file") == expected
        assert await read(archive, archive.getinfo("file"), into=True) == expected
        # A plain stdlib ZipInfo is accepted as well as RemoteZipInfo.
        assert await read(archive, reference.getinfo("file")) == expected


@pytest.mark.parametrize("prefix", [b"", b"#!/usr/bin/env python3\n"])
@pytest.mark.parametrize("is_zip64", [False, True])
async def test_prepended_data(kind: str, prefix: bytes, is_zip64: bool) -> None:
    data = make_zip({"file": b"payload"})
    if is_zip64:
        data = zip64(data)
    data = prefix + data
    with zipfile.ZipFile(io.BytesIO(data)) as reference:
        expected = reference.read("file")
        offset = reference.getinfo("file").header_offset
    async with opened(data, kind) as archive:
        assert archive.getinfo("file").header_offset == offset
        assert archive.eocd_info.archive_offset == len(prefix)
        assert await read(archive, "file") == expected
        assert await read(archive, "file", into=True) == expected


async def test_eocd_signature_in_offset(kind: str, tmp_path: Path) -> None:
    path = tmp_path / "demo-1.0-py3-none-any.whl"
    cd_offset = int.from_bytes(b"PK\x05\x06", "little")
    # A sparse prefix puts signature bytes in a real offset without
    # allocating or writing a 96 MiB test fixture.
    with path.open("w+b") as dest:
        dest.seek(cd_offset - 30 - len("file") - len(b"payload"))
        with zipfile.ZipFile(dest, "w") as writer:
            writer.writestr("file", b"payload")
    with zipfile.ZipFile(path) as reference:
        expected = reference.read("file")
    async with AsyncExitStack() as stack:
        if kind.startswith("async"):
            cls = AsyncRemoteWheel if kind.endswith("wheel") else AsyncRemoteZip
            archive = await stack.enter_async_context(cls(AsyncFileReader(path)))
        else:
            cls = SyncRemoteWheel if kind.endswith("wheel") else SyncRemoteZip
            archive = stack.enter_context(cls(FileReader(path)))
        assert archive.eocd_info.cd_offset == cd_offset
        assert await read(archive, "file") == expected
        assert await read(archive, "file", into=True) == expected


async def test_eocd_signature_in_payload_with_trailing_data(kind: str) -> None:
    placeholder = b"\0" * EOCD_STRUCT.size + b"payload"
    data = make_zip({"file": placeholder})
    member_start = 30 + len("file")
    # This false record's comment covers the real EOCD but not the end
    # of the archive. It must not replace the last signature found.
    comment_length = len(data) - 1 - member_start - EOCD_STRUCT.size
    embedded = EOCD_STRUCT.pack(b"PK\x05\x06", 0, 0, 0, 0, 0, 0, comment_length)
    payload = embedded + b"payload"
    data = make_zip({"file": payload}) + b"trailing data"
    with zipfile.ZipFile(io.BytesIO(data)) as reference:
        expected = reference.read("file")
    async with opened(data, kind) as archive:
        assert archive.namelist() == ["file"]
        assert await read(archive, "file") == expected == payload
        assert await read(archive, "file", into=True) == expected


async def test_eocd_search_window(kind: str) -> None:
    data = make_zip({"file": b"payload"}) + b"x" * 66000
    with pytest.raises(zipfile.BadZipFile), zipfile.ZipFile(io.BytesIO(data)):
        pass
    # Wheel readers cache more data, but must use the same search window.
    with pytest.raises(zipfile.BadZipFile):
        async with opened(data, kind):
            pass


@pytest.mark.parametrize("is_zip64", [False, True])
@pytest.mark.parametrize("trailer", [b"", b"trailing data"])
@pytest.mark.parametrize(
    "comment", [b"PK\x05\x06", b"PK\x05\x06" + b"\0" * 24], ids=["short", "binary"]
)
async def test_eocd_signature_in_comment(
    kind: str, is_zip64: bool, trailer: bytes, comment: bytes
) -> None:
    data = make_zip({"file": b"payload"})
    if is_zip64:
        data = zip64(data, comment=comment)
    else:
        data = data[:-2] + struct.pack("<H", len(comment)) + comment
    data += trailer
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as reference:
            names = reference.namelist()
            expected_comment = reference.comment
    except zipfile.BadZipFile:
        with pytest.raises(zipfile.BadZipFile):
            async with opened(data, kind):
                pass
    else:
        async with opened(data, kind) as archive:
            assert archive.namelist() == names
            assert archive.comment == expected_comment


@pytest.mark.parametrize("field", [4, 6, 8, 10])
async def test_zip64_legacy_placeholders(kind: str, field: int) -> None:
    data = bytearray(zip64(make_zip({"file": b"payload"})))
    end = len(data) - EOCD_STRUCT.size
    struct.pack_into("<HHHH", data, end + 4, 0, 0, 1, 1)
    struct.pack_into("<H", data, end + field, 0xFFFF)
    with zipfile.ZipFile(io.BytesIO(data)) as reference:
        expected = reference.read("file")
    async with opened(bytes(data), kind) as archive:
        assert await read(archive, "file") == expected
        assert await read(archive, "file", into=True) == expected


@pytest.mark.parametrize("prefix", [b"", b"#!/usr/bin/env python3\n"])
async def test_zip64_inconsistent_directory_offset(kind: str, prefix: bytes) -> None:
    data = bytearray(zip64(make_zip({"file": b"payload"})))
    record = len(data) - EOCD_STRUCT.size - ZIP64_EOCD_LOCATOR_STRUCT.size - ZIP64_EOCD_STRUCT.size
    offset = struct.unpack_from("<Q", data, record + 48)[0]
    struct.pack_into("<Q", data, record + 48, offset - 1)
    data = prefix + data
    with pytest.raises(zipfile.BadZipFile), zipfile.ZipFile(io.BytesIO(data)):
        pass
    with pytest.raises(zipfile.BadZipFile, match="ZIP64 central directory"):
        async with opened(data, kind):
            pass


@pytest.mark.parametrize(("field", "value"), [(4, 1), (16, 2)])
async def test_multidisk_archive(kind: str, field: int, value: int) -> None:
    data = bytearray(zip64(make_zip({"file": b"payload"})))
    locator = len(data) - EOCD_STRUCT.size - ZIP64_EOCD_LOCATOR_STRUCT.size
    struct.pack_into("<I", data, locator + field, value)
    with (
        pytest.raises(zipfile.BadZipFile, match="multiple disks"),
        zipfile.ZipFile(io.BytesIO(data)),
    ):
        pass
    with pytest.raises(zipfile.BadZipFile, match="multiple disks"):
        async with opened(bytes(data), kind):
            pass


@pytest.mark.parametrize(
    "fault",
    [
        "disk",
        "directory_disk",
        "count",
        "total_count",
        "entry_disk",
        "member_comment",
        "archive_comment",
        "negative_archive_offset",
        "zip64_disk",
        "zip64_directory_disk",
        "zip64_count",
        "zip64_disks_zero",
    ],
)
async def test_tolerated_directory_metadata(kind: str, fault: str) -> None:
    data = make_zip({"file": b"payload"})
    cd = EOCD_STRUCT.unpack_from(data, len(data) - EOCD_STRUCT.size)[6]
    record = len(data) - EOCD_STRUCT.size
    data = bytearray(zip64(data) if fault.startswith("zip64_") else data)
    if fault == "disk":
        struct.pack_into("<H", data, record + 4, 1)
    elif fault == "directory_disk":
        struct.pack_into("<H", data, record + 6, 1)
    elif fault == "count":
        struct.pack_into("<H", data, record + 8, 2)
    elif fault == "total_count":
        struct.pack_into("<H", data, record + 10, 0)
    elif fault == "entry_disk":
        struct.pack_into("<H", data, cd + 34, 1)
    elif fault == "member_comment":
        struct.pack_into("<H", data, cd + 32, 10)
    elif fault == "archive_comment":
        struct.pack_into("<H", data, record + 20, 10)
        data.extend(b"short")
    elif fault == "negative_archive_offset":
        struct.pack_into("<I", data, record + 16, cd + 100)
        struct.pack_into("<I", data, cd + 42, 100)
    elif fault == "zip64_disk":
        struct.pack_into("<I", data, record + 16, 1)
    elif fault == "zip64_directory_disk":
        struct.pack_into("<I", data, record + 20, 1)
    elif fault == "zip64_count":
        struct.pack_into("<Q", data, record + 24, 2)
    else:
        struct.pack_into("<I", data, record + ZIP64_EOCD_STRUCT.size + 16, 0)
    with zipfile.ZipFile(io.BytesIO(data)) as reference:
        expected = reference.read("file")
        info = reference.getinfo("file")
        comment = reference.comment
    async with opened(bytes(data), kind) as archive:
        actual = archive.getinfo("file")
        assert actual.volume == info.volume
        assert actual.comment == info.comment
        assert archive.comment == comment
        assert await read(archive, "file") == expected
        assert await read(archive, "file", into=True) == expected


@pytest.mark.parametrize("disk_data", [b"", b"\0", struct.pack("<I", 42)])
async def test_zip64_disk_number_is_ignored(kind: str, disk_data: bytes) -> None:
    info = zipfile.ZipInfo("file")
    info.extra = struct.pack("<HH", 1, len(disk_data)) + disk_data
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as writer:
        writer.writestr(info, b"payload")
    data = bytearray(buffer.getvalue())
    cd = EOCD_STRUCT.unpack_from(data, len(data) - EOCD_STRUCT.size)[6]
    struct.pack_into("<H", data, cd + 34, 0xFFFF)
    with zipfile.ZipFile(io.BytesIO(data)) as reference:
        expected_volume = reference.getinfo("file").volume
        expected = reference.read("file")
    async with opened(bytes(data), kind) as archive:
        assert archive.getinfo("file").volume == expected_volume
        assert await read(archive, "file") == expected
        assert await read(archive, "file", into=True) == expected


@pytest.mark.parametrize("truncated", [False, True])
async def test_repeated_zip64_fields(kind: str, truncated: bool) -> None:
    payload = b"payload"
    compressor = zlib.compressobj(wbits=-15)
    compressed_size = len(compressor.compress(payload) + compressor.flush())
    info = zipfile.ZipInfo("file")
    info.compress_type = zipfile.ZIP_DEFLATED
    # A later ZIP64 field can replace both 32-bit and 64-bit sentinels.
    first = struct.pack("<QQQ", 0xFFFFFFFFFFFFFFFF, 0xFFFFFFFF, 0xFFFFFFFF)
    second = struct.pack("<QQQ", len(payload), compressed_size, 0)
    if truncated:
        second = second[:-1]
    info.extra = struct.pack("<HH", 1, len(first)) + first
    info.extra += struct.pack("<HH", 1, len(second)) + second
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as writer:
        writer.writestr(info, payload)
    data = bytearray(buffer.getvalue())
    cd = EOCD_STRUCT.unpack_from(data, len(data) - EOCD_STRUCT.size)[6]
    struct.pack_into("<II", data, cd + 20, 0xFFFFFFFF, 0xFFFFFFFF)
    struct.pack_into("<I", data, cd + 42, 0xFFFFFFFF)
    if truncated:
        with pytest.raises(zipfile.BadZipFile), zipfile.ZipFile(io.BytesIO(data)):
            pass
        with pytest.raises(zipfile.BadZipFile):
            async with opened(bytes(data), kind):
                pass
        return
    with zipfile.ZipFile(io.BytesIO(data)) as reference:
        expected_info = reference.getinfo("file")
        expected = reference.read("file")
    async with opened(bytes(data), kind) as archive:
        actual = archive.getinfo("file")
        assert actual.file_size == expected_info.file_size
        assert actual.compress_size == expected_info.compress_size
        assert actual.header_offset == expected_info.header_offset
        assert await read(archive, "file") == expected == payload
        assert await read(archive, "file", into=True) == expected


@pytest.mark.parametrize(("count", "offset"), [(1, 0), (0xFFFF, 0xFFFFFFFF)])
async def test_empty_directory_ignores_entry_count(kind: str, count: int, offset: int) -> None:
    data = EOCD_STRUCT.pack(b"PK\x05\x06", 0, 0, count, count, 0, offset, 0)
    with zipfile.ZipFile(io.BytesIO(data)) as reference:
        expected = reference.namelist()
    async with opened(data, kind) as archive:
        assert archive.namelist() == expected == []


async def test_duplicate_header_references(kind: str) -> None:
    data = make_zip({"file": b"payload"})
    fields = list(EOCD_STRUCT.unpack_from(data, len(data) - EOCD_STRUCT.size))
    cd = fields[6]
    entry = data[cd : -EOCD_STRUCT.size]
    fields[3] = fields[4] = 2
    fields[5] *= 2
    data = data[:cd] + entry + entry + EOCD_STRUCT.pack(*fields)
    message = "Overlapped entries: 'file' (possible zip bomb)"
    with zipfile.ZipFile(io.BytesIO(data)) as reference:
        # Python 3.12+ warns and reads; 3.11 rejects the first reference.
        if sys.version_info >= (3, 12):
            with warnings.catch_warnings(record=True) as recorded:
                warnings.simplefilter("always")
                assert [reference.read(info) for info in reference.infolist()] == [
                    b"payload",
                    b"payload",
                ]
            assert [str(w.message) for w in recorded] == [message]
    async with opened(data, kind) as archive:
        for into in (False, True):
            with warnings.catch_warnings(record=True) as recorded:
                warnings.simplefilter("always")
                assert [await read(archive, info, into=into) for info in archive.infolist()] == [
                    b"payload",
                    b"payload",
                ]
            assert [str(w.message) for w in recorded] == [message]


@pytest.mark.parametrize("extension", [b"", b"\0" * 70000])
async def test_zip64_long_comment(kind: str, extension: bytes) -> None:
    data = zip64(make_zip({"file": b"payload"}), comment=b"c" * 65535, extension=extension)
    with zipfile.ZipFile(io.BytesIO(data)) as reference:
        expected = reference.read("file")
        comment = reference.comment
    async with opened(data, kind) as archive:
        assert archive.comment == comment
        assert await read(archive, "file") == expected
        assert await read(archive, "file", into=True) == expected


@pytest.fixture(scope="module", params=[65535, 65536])
def many_entries(request: pytest.FixtureRequest) -> tuple[bytes, int]:
    count = request.param
    return make_zip({str(number): b"" for number in range(count)}), count


async def test_entry_count_boundary(kind: str, many_entries: tuple[bytes, int]) -> None:
    data, count = many_entries
    with zipfile.ZipFile(io.BytesIO(data)) as reference:
        names = reference.namelist()
    async with opened(data, kind) as archive:
        assert len(archive.infolist()) == count
        assert archive.namelist() == names
        assert await read(archive, str(count - 1)) == b""


@pytest.mark.parametrize("method", [zipfile.ZIP_DEFLATED, zipfile.ZIP_LZMA])
async def test_data_descriptor_and_zip64_entry(kind: str, method: int) -> None:
    buffer = Unseekable()
    with (
        zipfile.ZipFile(buffer, "w", compression=method) as writer,
        writer.open("file", "w", force_zip64=True) as dest,
    ):
        dest.write(b"payload")
    data = buffer.getvalue()
    with zipfile.ZipFile(io.BytesIO(data)) as reference:
        expected = reference.read("file")
    async with opened(data, kind) as archive:
        assert archive.getinfo("file").flag_bits & 8
        assert await read(archive, "file") == expected
        assert await read(archive, "file", into=True) == expected


@pytest.mark.parametrize("size", [0, 1, 100000])
async def test_lzma_without_end_marker(kind: str, size: int) -> None:
    payload = b"x" * size
    original = make_zip({"file": payload}, compression=zipfile.ZIP_LZMA)
    cd = EOCD_STRUCT.unpack_from(original, len(original) - EOCD_STRUCT.size)[6]
    # The size in the directory terminates LZMA entries when flag bit 1
    # is clear, so remove the end marker and update the compressed extent.
    data = bytearray(original[: cd - 5] + original[cd:])
    cd -= 5
    struct.pack_into("<H", data, 6, 0)
    struct.pack_into("<H", data, cd + 8, 0)
    for offset in (18, cd + 20):
        struct.pack_into("<I", data, offset, struct.unpack_from("<I", data, offset)[0] - 5)
    struct.pack_into("<I", data, len(data) - 6, cd)
    with zipfile.ZipFile(io.BytesIO(data)) as reference:
        assert reference.read("file") == payload
    async with opened(bytes(data), kind) as archive:
        assert await read(archive, "file") == payload
        assert await read(archive, "file", into=True) == payload


@pytest.mark.parametrize(
    ("properties_size", "properties", "compressed_size"),
    [
        (0, 93, None),
        (4, 93, None),
        (6, 93, None),
        (5, 225, None),
        (5, 13, None),  # Valid property byte, but unsupported lc + lp.
        (4, 93, 4),
        (5, 225, 9),
        (5, 13, 9),
        (65535, 93, None),
        (5, 93, 9),
        (5, 93, 10),
    ],
)
async def test_lzma_properties(
    kind: str, properties_size: int, properties: int, compressed_size: int | None
) -> None:
    # An empty member lets us compare stdlib's tolerance of incomplete
    # properties without a CRC failure hiding the header's behavior.
    data = bytearray(make_zip({"file": b""}, compression=zipfile.ZIP_LZMA))
    start = 30 + len("file")
    struct.pack_into("<HB", data, start + 2, properties_size, properties)
    if compressed_size is not None:
        cd = EOCD_STRUCT.unpack_from(data, len(data) - EOCD_STRUCT.size)[6]
        struct.pack_into("<I", data, cd + 20, compressed_size)
    error: type[Exception] | None = None
    expected: bytes | None = None
    with zipfile.ZipFile(io.BytesIO(data)) as reference:
        try:
            expected = reference.read("file")
        except Exception as exc:
            error = type(exc)
    async with opened(bytes(data), kind) as archive:
        for into in (False, True):
            if error is not None:
                with pytest.raises(error):
                    await read(archive, "file", into=into)
            else:
                assert await read(archive, "file", into=into) == expected == b""


@pytest.mark.parametrize(
    "method",
    [
        zipfile.ZIP_DEFLATED,
        zipfile.ZIP_BZIP2,
        zipfile.ZIP_LZMA,
        pytest.param(93, marks=needs_zstd),
    ],
)
async def test_truncated_compressed_stream(kind: str, method: int) -> None:
    data = bytearray(make_zip({"file": b"payload" * 100}, compression=method))
    cd = EOCD_STRUCT.unpack_from(data, len(data) - EOCD_STRUCT.size)[6]
    size = struct.unpack_from("<I", data, cd + 20)[0]
    struct.pack_into("<I", data, cd + 20, size - 1)
    with zipfile.ZipFile(io.BytesIO(data)) as reference:
        try:
            expected = reference.read("file")
        except zipfile.BadZipFile:
            expected = None
    async with opened(bytes(data), kind) as archive:
        for into in (False, True):
            if expected is None:
                with pytest.raises(zipfile.BadZipFile):
                    await read(archive, "file", into=into)
            else:
                assert await read(archive, "file", into=into) == expected == b"payload" * 100


@pytest.mark.parametrize(
    "method",
    [0, 8, 12, 14, pytest.param(93, marks=needs_zstd)],
)
@pytest.mark.parametrize("declared_size", [0, 7, 1000])
async def test_declared_output_size(kind: str, method: int, declared_size: int) -> None:
    payload = b"payload" * 100
    data = bytearray(make_zip({"file": payload}, compression=method))
    cd = EOCD_STRUCT.unpack_from(data, len(data) - EOCD_STRUCT.size)[6]
    struct.pack_into("<I", data, cd + 24, declared_size)
    struct.pack_into("<I", data, cd + 16, zlib.crc32(payload[:declared_size]))
    with zipfile.ZipFile(io.BytesIO(data)) as reference:
        expected = reference.read("file")
    async with opened(bytes(data), kind) as archive:
        assert await read(archive, "file") == expected
        assert await read(archive, "file", into=True) == expected
    # Tolerance still requires a correct CRC for the returned bytes.
    struct.pack_into("<I", data, cd + 16, 0xDEADBEEF)
    with pytest.raises(zipfile.BadZipFile), zipfile.ZipFile(io.BytesIO(data)) as reference:
        reference.read("file")
    async with opened(bytes(data), kind) as archive:
        for into in (False, True):
            with pytest.raises(zipfile.BadZipFile):
                await read(archive, "file", into=into)


@pytest.fixture(
    scope="module",
    params=[
        (zipfile.ZIP_BZIP2, False),
        (zipfile.ZIP_BZIP2, True),
        (zipfile.ZIP_LZMA, False),
        (zipfile.ZIP_LZMA, True),
        pytest.param((93, False), marks=needs_zstd),
        pytest.param((93, True), marks=needs_zstd),
    ],
)
def oversized_compressed_member(request: pytest.FixtureRequest) -> bytes:
    method, streamed = request.param
    info = zipfile.ZipInfo("file")
    info.compress_type = method
    if streamed:
        # Keep the payload out of both header prefetches and the wheel cache.
        info.extra = struct.pack("<HH", 0xCAFE, 2048) + b"x" * 2048
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as writer:
        # More than two output chunks, with a trailer beyond the first chunk.
        writer.writestr(info, b"xyz" * 1500000)
        if streamed:
            writer.writestr("padding", b"x" * 200000)
    return buffer.getvalue()


@pytest.mark.parametrize("declared_size", [0, 1])
@pytest.mark.parametrize("corrupt", [False, True])
async def test_codec_validation_past_output_size(
    kind: str, oversized_compressed_member: bytes, declared_size: int, corrupt: bool
) -> None:
    data = bytearray(oversized_compressed_member)
    with zipfile.ZipFile(io.BytesIO(data)) as reference:
        info = reference.getinfo("file")
        cd = reference.start_dir
    expected = b"x"[:declared_size]
    struct.pack_into("<I", data, cd + 24, declared_size)
    struct.pack_into("<I", data, cd + 16, zlib.crc32(expected))
    if corrupt:
        start = info.header_offset + 30 + len(info.filename) + len(info.extra)
        data[start + info.compress_size - 1] ^= 0x80
    expected_error: type[Exception] | None = None
    with zipfile.ZipFile(io.BytesIO(data)) as reference:
        if corrupt:
            with pytest.raises(Exception) as error:
                reference.read("file")
            expected_error = type(error.value)
        else:
            assert reference.read("file") == expected
    async with opened(bytes(data), kind) as archive:
        for into in (False, True):
            if expected_error is not None:
                with pytest.raises(expected_error):
                    await read(archive, "file", into=into)
            else:
                assert await read(archive, "file", into=into) == expected


@pytest.mark.parametrize(
    ("method", "module"),
    [(8, "zlib"), (12, "bz2"), (14, "lzma"), pytest.param(93, "zstd", marks=needs_zstd)],
)
async def test_missing_codec(
    kind: str, method: int, module: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = make_zip({"file": b"payload"}, compression=method)
    monkeypatch.setattr(zipfile, module, None)
    monkeypatch.setattr(decoding, module, None)
    with zipfile.ZipFile(io.BytesIO(data)) as reference, pytest.raises(RuntimeError) as error:
        reference.read("file")
    async with opened(data, kind) as archive:
        for into in (False, True):
            with pytest.raises(RuntimeError) as actual_error:
                await read(archive, "file", into=into)
            assert str(actual_error.value) == str(error.value)


@pytest.mark.parametrize("method", [0, 12, 14])
def test_import_without_zlib(method: int, tmp_path: Path) -> None:
    path = tmp_path / "demo-1.0-py3-none-any.whl"
    path.write_bytes(make_zip({"file": b"payload"}, compression=method))
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import sys
sys.modules['zlib'] = None
import asyncio
import io
import zipfile
from zipwire import AsyncRemoteWheel, AsyncRemoteZip, SyncRemoteWheel, SyncRemoteZip
from zipwire.backends import AsyncFileReader, FileReader

async def check():
    path = sys.argv[1]
    with zipfile.ZipFile(path) as reference:
        expected = reference.read('file')
    for cls in (SyncRemoteZip, SyncRemoteWheel):
        with cls(FileReader(path)) as archive:
            assert archive.read('file') == expected
            dest = io.BytesIO()
            archive.read_into('file', dest)
            assert dest.getvalue() == expected
    for cls in (AsyncRemoteZip, AsyncRemoteWheel):
        async with cls(AsyncFileReader(path)) as archive:
            assert await archive.read('file') == expected
            dest = io.BytesIO()
            await archive.read_into('file', dest)
            assert dest.getvalue() == expected

asyncio.run(check())
""",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("password", [None, b"", "", 0, False, [], {}])
async def test_clear_password(kind: str, password: object) -> None:
    data = base64.b64decode(_ENCRYPTED["stored"])
    with zipfile.ZipFile(io.BytesIO(data)) as reference:
        reference.setpassword(b"test")
        reference.setpassword(password)  # type: ignore[arg-type]
        expected = reference.pwd
    async with opened(data, kind) as archive:
        archive.setpassword(b"test")
        archive.setpassword(password)  # type: ignore[arg-type]
        assert archive.pwd is expected is None
        with pytest.raises(RuntimeError, match="password"):
            await read(archive, "file.txt")


@pytest.mark.parametrize("method", _ENCRYPTED)
async def test_passwords(kind: str, method: str) -> None:
    buffer = io.BytesIO(base64.b64decode(_ENCRYPTED[method]))
    # Leave enough trailing data to exercise the wheel's cached reads.
    with zipfile.ZipFile(buffer, "a") as writer:
        writer.writestr("padding", b"x" * 2048)
    data = buffer.getvalue()
    with zipfile.ZipFile(io.BytesIO(data)) as reference:
        assert reference.read("file.txt", pwd=b"test") == _ENCRYPTED_CONTENT
        with pytest.raises(RuntimeError) as error:
            reference.read("file.txt", pwd=b"wrong")
    async with opened(data, kind) as archive:
        reader = archive._reader
        assert isinstance(reader, (MockSyncReader, MockAsyncReader))
        read_count = reader.read_count
        for into in (False, True):
            with pytest.raises(RuntimeError, match="password"):
                await read(archive, "file.txt", into=into)
            with pytest.raises(RuntimeError) as actual_error:
                await read(archive, "file.txt", pwd=b"wrong", into=into)
            assert str(actual_error.value) == str(error.value)
            assert await read(archive, "file.txt", pwd=b"test", into=into) == _ENCRYPTED_CONTENT
            archive.setpassword(b"test")
            assert await read(archive, "file.txt", into=into) == _ENCRYPTED_CONTENT
            archive.setpassword(None)
        with pytest.raises(TypeError, match="bytes"):
            archive.setpassword("test")  # type: ignore[arg-type]
        if kind.endswith("wheel"):
            assert reader.read_count == read_count


@pytest.mark.parametrize("method", _ENCRYPTED)
@pytest.mark.parametrize("chunk_size", [1, 7, 13])
def test_encrypted_chunk_boundaries(method: str, chunk_size: int) -> None:
    data = base64.b64decode(_ENCRYPTED[method])
    with zipfile.ZipFile(io.BytesIO(data)) as reference:
        info = reference.getinfo("file.txt")
    filename_len, extra_len = struct.unpack_from("<HH", data, info.header_offset + 26)
    start = info.header_offset + 30 + filename_len + extra_len
    compressed = data[start : start + info.compress_size]
    raw_time = struct.unpack_from("<H", data, info.header_offset + 10)[0]
    check = (raw_time >> 8) & 0xFF if info.flag_bits & 8 else info.CRC >> 24
    dest = io.BytesIO()
    decoder = StreamingDecompressor(
        info.compress_type,
        info.CRC,
        dest,
        expected_size=info.file_size,
        pwd=b"test",
        encryption_check=check,
    )
    for offset in range(0, len(compressed), chunk_size):
        decoder.feed(compressed[offset : offset + chunk_size])
    decoder.finish()
    assert dest.getvalue() == _ENCRYPTED_CONTENT


@pytest.mark.parametrize("valid_crc", [False, True])
async def test_unicode_path(kind: str, valid_crc: bool) -> None:
    legacy = "cafe.txt"
    unicode_name = "café.txt"
    info = zipfile.ZipInfo(legacy)
    encoded = unicode_name.encode()
    crc = zlib.crc32(legacy.encode()) if valid_crc else 0
    info.extra = struct.pack("<HHBI", 0x7075, len(encoded) + 5, 1, crc) + encoded
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as writer:
        writer.writestr(info, b"payload")
    async with opened(buffer.getvalue(), kind) as archive:
        name = unicode_name if valid_crc else legacy
        assert archive.namelist() == [name]
        assert archive.getinfo(name).orig_filename == legacy
        assert await read(archive, name) == b"payload"
        assert await read(archive, name, into=True) == b"payload"


@pytest.mark.parametrize("zip64_first", [False, True])
async def test_combined_extra_fields(kind: str, zip64_first: bool) -> None:
    legacy = "cafe.txt"
    name = "café.txt"
    payload = b"payload"
    encoded = name.encode()
    unicode_extra = (
        struct.pack("<HHBI", 0x7075, len(encoded) + 5, 1, zlib.crc32(legacy.encode())) + encoded
    )
    zip64_extra = struct.pack("<HHQQQ", 1, 24, len(payload), len(payload), 0)
    info = zipfile.ZipInfo(legacy)
    info.extra = zip64_extra + unicode_extra if zip64_first else unicode_extra + zip64_extra
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as writer:
        writer.writestr(info, payload)
    data = bytearray(buffer.getvalue())
    cd = EOCD_STRUCT.unpack_from(data, len(data) - EOCD_STRUCT.size)[6]
    struct.pack_into("<II", data, cd + 20, 0xFFFFFFFF, 0xFFFFFFFF)
    struct.pack_into("<I", data, cd + 42, 0xFFFFFFFF)
    with zipfile.ZipFile(io.BytesIO(data)) as reference:
        expected_info = reference.infolist()[0]
        expected = reference.read(expected_info)
    async with opened(bytes(data), kind) as archive:
        actual = archive.getinfo(name)
        assert actual.orig_filename == legacy
        assert actual.file_size == expected_info.file_size
        assert actual.compress_size == expected_info.compress_size
        assert actual.header_offset == expected_info.header_offset
        assert await read(archive, name) == expected == payload
        assert await read(archive, name, into=True) == expected


async def test_metadata_encoding(kind: str) -> None:
    data = make_zip({"café.txt": b"payload"})
    raw_name = "café.txt".encode()
    # Keep the name length fixed while replacing UTF-8 with a legacy name.
    data = bytearray(data.replace(raw_name, b"caf\xe9x.txt"))
    cd = EOCD_STRUCT.unpack_from(data, len(data) - EOCD_STRUCT.size)[6]
    struct.pack_into("<H", data, 6, 0)
    struct.pack_into("<H", data, cd + 8, 0)
    with zipfile.ZipFile(io.BytesIO(data), metadata_encoding="cp1252") as reference:
        name = reference.namelist()[0]
        expected = reference.read(name)
    async with opened(bytes(data), kind, metadata_encoding="cp1252") as archive:
        assert archive.namelist() == [name]
        assert await read(archive, name) == expected
    utf8 = make_zip({"café.txt": b"utf8"})
    async with opened(utf8, kind, metadata_encoding="ascii") as archive:
        assert await read(archive, "café.txt") == b"utf8"


async def test_directory_with_data_and_long_header(kind: str) -> None:
    name = "a" * 5000
    data = make_zip({name: b"payload", "directory/": b"directory payload"})
    with zipfile.ZipFile(io.BytesIO(data)) as reference:
        async with opened(data, kind) as archive:
            for entry in reference.infolist():
                assert await read(archive, entry.filename) == reference.read(entry)
                assert await read(archive, entry.filename, into=True) == reference.read(entry)


@pytest.mark.parametrize(
    "fault", ["name", "patched", "strong", "version", "overlap", "size", "extra"]
)
async def test_reject_invalid_entries(kind: str, fault: str) -> None:
    payload = b"payload"
    data = bytearray(make_zip({"file.txt": payload, "padding": b"x" * 2048}))
    cd = EOCD_STRUCT.unpack_from(data, len(data) - EOCD_STRUCT.size)[6]
    expected_error: type[Exception] = zipfile.BadZipFile
    if fault == "name":
        data[30:38] = b"fake.txt"
    elif fault in ("patched", "strong"):
        flag = 0x20 if fault == "patched" else 0x40
        struct.pack_into("<H", data, cd + 8, flag)
        expected_error = NotImplementedError
    elif fault == "version":
        struct.pack_into("<H", data, cd + 6, 99)
        expected_error = NotImplementedError
    elif fault == "overlap":
        expanded = payload + b"P"
        struct.pack_into("<III", data, cd + 16, zlib.crc32(expanded), len(expanded), len(expanded))
    elif fault == "size":
        struct.pack_into("<I", data, cd + 24, 1)
    else:
        info = zipfile.ZipInfo("file.txt")
        info.extra = struct.pack("<HH", 1, 8)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as writer:
            writer.writestr(info, payload)
        data = buffer.getvalue()
    with pytest.raises(expected_error), zipfile.ZipFile(io.BytesIO(data)) as reference:
        reference.read("file.txt")
    for into in (False, True):
        with pytest.raises(expected_error):
            async with opened(bytes(data), kind) as archive:
                reader = archive._reader
                assert isinstance(reader, (MockSyncReader, MockAsyncReader))
                read_count = reader.read_count
                try:
                    await read(archive, "file.txt", into=into)
                finally:
                    if kind.endswith("wheel"):
                        assert reader.read_count == read_count


@pytest.mark.parametrize("method", [zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED])
@pytest.mark.parametrize("write_limit", [None, 3])
async def test_partial_writes(kind: str, method: int, write_limit: int | None) -> None:
    payload = Random(0).randbytes(70000)
    data = make_zip({"file": payload}, compression=method)
    output = bytearray()

    class Destination:
        def write(self, chunk: bytes) -> int | None:
            assert isinstance(chunk, bytes)
            size = len(chunk) if write_limit is None else min(write_limit, len(chunk))
            output.extend(chunk[:size])
            return None if write_limit is None else size

    async with opened(data, kind) as archive:
        if isinstance(archive, AsyncRemoteZip):
            await archive.read_into("file", Destination())
        else:
            archive.read_into("file", Destination())
    assert output == payload


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("failure", ["raise", "zero", "negative", "oversized", "noninteger"])
async def test_stream_closed_after_write_failure(asynchronous: bool, failure: str) -> None:
    data = make_zip({"file": b"x" * 70000})
    stream_closed = False

    class Reader(MockSyncReader):
        def stream_range(self, offset: int, length: int) -> Iterator[bytes]:
            nonlocal stream_closed
            try:
                yield from super().stream_range(offset, length)
            finally:
                stream_closed = True

    class AsyncReader(MockAsyncReader):
        async def stream_range(self, offset: int, length: int) -> AsyncIterator[bytes]:
            nonlocal stream_closed
            try:
                async for chunk in super().stream_range(offset, length):
                    yield chunk
            finally:
                stream_closed = True

    class Destination:
        def write(self, chunk: bytes) -> int:
            if failure == "raise":
                raise OSError("write failed")
            if failure == "zero":
                return 0
            if failure == "negative":
                return -1
            if failure == "oversized":
                return len(chunk) + 1
            return typing.cast("int", "invalid")

    if asynchronous:
        async with AsyncRemoteZip(AsyncReader(data)) as archive:
            with pytest.raises(OSError, match="write"):
                await archive.read_into("file", Destination())
            assert stream_closed
    else:
        with SyncRemoteZip(Reader(data)) as archive:
            with pytest.raises(OSError, match="write"):
                archive.read_into("file", Destination())
            assert stream_closed


@needs_zstd
async def test_legacy_zstandard(kind: str) -> None:
    data = bytearray(make_zip({"file": b"payload"}, compression=93))
    cd = EOCD_STRUCT.unpack_from(data, len(data) - EOCD_STRUCT.size)[6]
    struct.pack_into("<H", data, 8, 20)
    struct.pack_into("<H", data, cd + 10, 20)
    async with opened(bytes(data), kind) as archive:
        assert await read(archive, "file") == b"payload"
        assert await read(archive, "file", into=True) == b"payload"


@pytest.mark.parametrize("method", [zipfile.ZIP_DEFLATED, zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA])
def test_bounded_decoder_output(method: int) -> None:
    payload = b"x" * (STREAM_CHUNK_SIZE * 2 + 1)
    data = make_zip({"file": payload}, compression=method)
    with zipfile.ZipFile(io.BytesIO(data)) as reference:
        info = reference.getinfo("file")
    compressed = data[34 : 34 + info.compress_size]
    writes: list[bytes] = []

    class Destination:
        def write(self, chunk: bytes) -> None:
            assert len(chunk) <= STREAM_CHUNK_SIZE
            writes.append(chunk)

    decoder = StreamingDecompressor(method, info.CRC, Destination(), expected_size=len(payload))
    decoder.feed(compressed)
    decoder.finish()
    assert b"".join(writes) == payload
