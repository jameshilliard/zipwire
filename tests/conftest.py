"""Test fixtures: generate ZIP files and provide mock readers."""

from __future__ import annotations

import io
import struct
import typing
import zipfile
from importlib.util import find_spec

import pytest

from zipwire._parser import find_eocd
from zipwire._zipstructs import CentralDirHeaderRecord, LocalFileHeaderRecord

has_httpx2 = find_spec("httpx2") is not None
has_requests = find_spec("requests") is not None
has_aiohttp = find_spec("aiohttp") is not None
has_anyio = find_spec("anyio") is not None
has_aiobotocore = find_spec("aiobotocore") is not None
has_zstd = find_spec("compression") is not None and find_spec("compression.zstd") is not None

needs_httpx2 = pytest.mark.skipif(not has_httpx2, reason="httpx2 not installed")
needs_requests = pytest.mark.skipif(not has_requests, reason="requests not installed")
needs_aiohttp = pytest.mark.skipif(not has_aiohttp, reason="aiohttp not installed")
needs_anyio = pytest.mark.skipif(not has_anyio, reason="anyio not installed")
needs_aiobotocore = pytest.mark.skipif(not has_aiobotocore, reason="aiobotocore not installed")
needs_zstd = pytest.mark.skipif(not has_zstd, reason="compression.zstd not available")


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--all-backends",
        action="store_true",
        default=False,
        help="Assert that all optional backends are installed.",
    )


def pytest_configure(config: pytest.Config) -> None:
    if config.getoption("all_backends"):
        missing = []
        if not has_httpx2:
            missing.append("httpx2")
        if not has_requests:
            missing.append("requests")
        if not has_aiohttp:
            missing.append("aiohttp")
        if not has_aiobotocore:
            missing.append("aiobotocore")
        if missing:
            raise pytest.UsageError(
                f"--all-backends: missing optional backends: {', '.join(missing)}"
            )


if typing.TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Generator


def make_zip(files: dict[str, bytes], *, compression: int = zipfile.ZIP_STORED) -> bytes:
    """Create an in-memory ZIP archive.

    Args:
        files: Mapping of filename to content.
        compression: ZIP compression method (ZIP_STORED or ZIP_DEFLATED).

    Returns:
        Raw bytes of the ZIP file.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=compression) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


def cd_entry_offset(data: bytes, index: int = 0) -> int:
    """Absolute offset of central directory entry *index* in *data*."""
    eocd = find_eocd(data, len(data))
    offset = eocd.cd_offset
    for _ in range(index):
        fn_len, extra_len, comment_len = struct.unpack_from("<HHH", data, offset + 28)
        offset += CentralDirHeaderRecord.FIXED_SIZE + fn_len + extra_len + comment_len
    return offset


def legacy_encoded_zip(name: str, encoding: str, content: bytes = b"legacy content") -> bytes:
    """A single-entry ZIP whose entry name is encoded with a legacy codec.

    The archive is written with an ASCII placeholder of the same byte
    length, then the raw *encoding* bytes are patched into the local
    file header and the central directory, leaving the UTF-8 flag
    unset so the name decodes as CP437 by default.
    """
    raw = name.encode(encoding)
    data = make_zip({"l" * len(raw): content})
    buf = bytearray(data)
    cd_off = cd_entry_offset(data)
    cd_fixed = CentralDirHeaderRecord.FIXED_SIZE
    buf[cd_off + cd_fixed : cd_off + cd_fixed + len(raw)] = raw
    header_offset = struct.unpack_from("<I", buf, cd_off + 42)[0]
    lfh_fixed = LocalFileHeaderRecord.FIXED_SIZE
    buf[header_offset + lfh_fixed : header_offset + lfh_fixed + len(raw)] = raw
    return bytes(buf)


class MockHeaders:
    """Lower-case header mapping for mock readers.

    A plain dict would work at runtime, but pyrefly cannot match the
    overloaded ``get`` of the mapping types against the Headers protocol;
    declaring the protocol's signatures directly makes the mock readers
    verify against SyncReader and AsyncReader.
    """

    def __init__(self, headers: dict[str, str] | None = None) -> None:
        self._headers: dict[str, str] = dict(headers) if headers else {}

    def __getitem__(self, key: str, /) -> str:
        return self._headers[key]

    def __contains__(self, key: object, /) -> bool:
        return key in self._headers

    def get(self, key: str, default: str | None = None, /) -> str | None:
        return self._headers.get(key, default)


class MockSyncReader:
    """A SyncReader backed by in-memory bytes (simulates a remote ZIP)."""

    def __init__(self, data: bytes, url: str = "") -> None:
        self.data = data
        self.closed = False
        self.read_count = 0
        self.requests: list[tuple[int, int]] = []
        self._url = url

    @property
    def url(self) -> str:
        return self._url

    def head(self) -> MockHeaders:
        return MockHeaders(
            {
                "content-length": str(len(self.data)),
                "accept-ranges": "bytes",
            }
        )

    def read_range(
        self,
        offset: int,
        length: int,
    ) -> tuple[bytes, MockHeaders]:
        self.read_count += 1
        self.requests.append((offset, length))
        total = len(self.data)
        chunk = self.data[offset : offset + length]
        end = offset + len(chunk) - 1
        headers = MockHeaders({"content-range": f"bytes {offset}-{end}/{total}"})
        return chunk, headers

    def stream_range(self, offset: int, length: int) -> Generator[bytes, None, None]:
        data = self.data[offset : offset + length]
        chunk_size = 1024
        for pos in range(0, len(data), chunk_size):
            self.read_count += 1
            yield data[pos : pos + chunk_size]

    def close(self) -> None:
        self.closed = True


class MockAsyncReader:
    """An AsyncReader backed by in-memory bytes."""

    def __init__(self, data: bytes, url: str = "") -> None:
        self.data = data
        self.closed = False
        self.read_count = 0
        self.requests: list[tuple[int, int]] = []
        self._url = url

    @property
    def url(self) -> str:
        return self._url

    async def head(self) -> MockHeaders:
        return MockHeaders(
            {
                "content-length": str(len(self.data)),
                "accept-ranges": "bytes",
            }
        )

    async def read_range(
        self,
        offset: int,
        length: int,
    ) -> tuple[bytes, MockHeaders]:
        self.read_count += 1
        self.requests.append((offset, length))
        total = len(self.data)
        chunk = self.data[offset : offset + length]
        end = offset + len(chunk) - 1
        headers = MockHeaders({"content-range": f"bytes {offset}-{end}/{total}"})
        return chunk, headers

    async def stream_range(self, offset: int, length: int) -> AsyncGenerator[bytes, None]:
        data = self.data[offset : offset + length]
        chunk_size = 1024
        for pos in range(0, len(data), chunk_size):
            self.read_count += 1
            yield data[pos : pos + chunk_size]

    async def close(self) -> None:
        self.closed = True


# --- Fixtures ---


@pytest.fixture
def stored_zip() -> bytes:
    """ZIP with STORED files."""
    return make_zip(
        {
            "hello.txt": b"Hello, World!",
            "data/numbers.bin": b"\x01\x02\x03\x04\x05",
        }
    )


@pytest.fixture
def deflated_zip() -> bytes:
    """ZIP with DEFLATED files."""
    return make_zip(
        {
            "hello.txt": b"Hello, World!",
            "data/numbers.bin": b"\x01\x02\x03\x04\x05",
            "repeated.txt": b"AAAA" * 1000,
        },
        compression=zipfile.ZIP_DEFLATED,
    )


@pytest.fixture
def mixed_zip() -> bytes:
    """ZIP with both STORED and DEFLATED files."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            zipfile.ZipInfo("stored.txt"),
            b"stored content",
            compress_type=zipfile.ZIP_STORED,
        )
        zf.writestr(
            zipfile.ZipInfo("deflated.txt"),
            b"deflated " * 100,
            compress_type=zipfile.ZIP_DEFLATED,
        )
    return buf.getvalue()


@pytest.fixture
def unicode_zip() -> bytes:
    """ZIP with unicode filenames."""
    return make_zip(
        {
            "\u00e9\u00e0\u00fc.txt": b"unicode content",
            "\u20ac100/rates.txt": b"hello",
        }
    )


@pytest.fixture
def directory_zip() -> bytes:
    """ZIP with explicit directory entries."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.mkdir("emptydir/")
        zf.writestr("emptydir/file.txt", b"inside dir")
    return buf.getvalue()


@pytest.fixture
def empty_zip() -> bytes:
    """ZIP with no files."""
    return make_zip({})


@pytest.fixture
def comment_zip() -> bytes:
    """ZIP with an archive comment."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("file.txt", b"content")
        zf.comment = b"This is a ZIP comment"
    return buf.getvalue()


@pytest.fixture(scope="session")
def zip64_zip() -> bytes:
    """ZIP with more than 65535 entries, forcing ZIP64 end records.

    Built once per session; the entry count overflows the classic
    EOCD's 16-bit field, so the archive carries a ZIP64 EOCD record
    and locator with sentinel values in the classic record.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        for i in range(65536 + 3):
            zf.writestr(f"f{i}.txt", b"x")
    return buf.getvalue()


@pytest.fixture
def large_file_zip() -> bytes:
    """ZIP with a STORED file exceeding PREFETCH_THRESHOLD (50 KiB)."""
    content = bytes(range(256)) * 208  # 53248 bytes = 52 KiB
    return make_zip({"large.bin": content})


@pytest.fixture
def zstandard_zip() -> bytes:
    """ZIP with ZSTANDARD-compressed files (requires Python 3.14+)."""
    if not hasattr(zipfile, "ZIP_ZSTANDARD"):
        pytest.skip("zipfile.ZIP_ZSTANDARD not available (requires Python 3.14+)")
    return make_zip(
        {
            "hello.txt": b"Hello, World!",
            "repeated.txt": b"AAAA" * 1000,
        },
        compression=zipfile.ZIP_ZSTANDARD,
    )
