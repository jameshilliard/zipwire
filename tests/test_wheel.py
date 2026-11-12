"""Tests for SyncRemoteWheel and AsyncRemoteWheel."""

from __future__ import annotations

import io
import zipfile

import pytest

from tests.conftest import MockAsyncReader, MockSyncReader
from zipwire import AsyncRemoteWheel, SyncRemoteWheel
from zipwire._errors import BadZipFile
from zipwire._wheel import _dist_info_from_url

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_wheel(
    name: str = "example_pkg",
    version: str = "1.0.0",
    extra_files: dict[str, bytes] | None = None,
    *,
    compression: int = zipfile.ZIP_STORED,
) -> tuple[bytes, str]:
    """Build a minimal wheel ZIP and return (data, wheel_filename).

    The archive contains:
      - {name}-{version}.dist-info/METADATA
      - {name}-{version}.dist-info/WHEEL
      - {name}-{version}.dist-info/RECORD
      - any extra_files (keyed by full archive path)
    """
    dist_info = f"{name}-{version}.dist-info"
    metadata = f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n".encode()
    wheel_file = b"Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    record = f"{dist_info}/METADATA,,\n{dist_info}/WHEEL,,\n{dist_info}/RECORD,,\n".encode()

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=compression) as zf:
        # Write extra files first so dist-info ends up at the end
        if extra_files:
            for path, content in extra_files.items():
                zf.writestr(path, content)
        zf.writestr(f"{dist_info}/METADATA", metadata)
        zf.writestr(f"{dist_info}/WHEEL", wheel_file)
        zf.writestr(f"{dist_info}/RECORD", record)

    filename = f"{name}-{version}-py3-none-any.whl"
    return buf.getvalue(), filename


def _make_wheel_distinfo_not_at_end(
    name: str = "example_pkg",
    version: str = "1.0.0",
) -> tuple[bytes, str]:
    """Build a wheel where dist-info is NOT at the end of the archive."""
    dist_info = f"{name}-{version}.dist-info"
    metadata = f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n".encode()
    wheel_file = b"Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    record = f"{dist_info}/METADATA,,\n{dist_info}/WHEEL,,\n{dist_info}/RECORD,,\n".encode()

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        # Write dist-info first
        zf.writestr(f"{dist_info}/METADATA", metadata)
        zf.writestr(f"{dist_info}/WHEEL", wheel_file)
        zf.writestr(f"{dist_info}/RECORD", record)
        # Write a large file after to push dist-info out of the tail
        zf.writestr("bigmodule.py", b"x = 1\n" * 20000)

    filename = f"{name}-{version}-py3-none-any.whl"
    return buf.getvalue(), filename


# ---------------------------------------------------------------------------
# Tests for _dist_info_from_url
# ---------------------------------------------------------------------------


class TestDeriveDistInfoPrefix:
    def test_simple(self) -> None:
        url = "https://files.pythonhosted.org/packages/ab/cd/requests-2.32.3-py3-none-any.whl"
        assert _dist_info_from_url(url) == "requests-2.32.3.dist-info/"

    def test_url_encoded(self) -> None:
        url = "https://example.com/my%20package-1.0.0-py3-none-any.whl"
        # Loose parsing accepts this; strict validation is not our job
        assert _dist_info_from_url(url) == "my package-1.0.0.dist-info/"

    def test_not_whl(self) -> None:
        url = "https://example.com/package-1.0.0.tar.gz"
        with pytest.raises(ValueError, match=r"does not point to a \.whl"):
            _dist_info_from_url(url)

    def test_malformed_filename(self) -> None:
        url = "https://example.com/nohyphens.whl"
        with pytest.raises(ValueError, match="Cannot parse wheel filename"):
            _dist_info_from_url(url)

    def test_with_build_tag(self) -> None:
        url = "https://example.com/pkg-1.0.0-1build-py3-none-any.whl"
        assert _dist_info_from_url(url) == "pkg-1.0.0.dist-info/"

    def test_complex_version(self) -> None:
        url = "https://example.com/my_pkg-2.0.0rc1-py3-none-any.whl"
        assert _dist_info_from_url(url) == "my_pkg-2.0.0rc1.dist-info/"

    def test_platform_specific(self) -> None:
        url = "https://example.com/numpy-1.26.4-cp312-cp312-manylinux_2_17_x86_64.manylinux2014_x86_64.whl"
        assert _dist_info_from_url(url) == "numpy-1.26.4.dist-info/"


# ---------------------------------------------------------------------------
# Sync tests
# ---------------------------------------------------------------------------


class TestSyncRemoteWheel:
    def test_enter_closes_reader_on_load_failure(self) -> None:
        url = "https://example.com/example_pkg-1.0.0-py3-none-any.whl"
        reader = MockSyncReader(b"not a zip file", url=url)
        with pytest.raises(BadZipFile), SyncRemoteWheel(reader):
            pass
        assert reader.closed

    def test_basic(self) -> None:
        data, filename = _make_wheel()
        reader = MockSyncReader(data, url=f"https://example.com/{filename}")
        with SyncRemoteWheel(reader) as whl:
            assert whl.dist_info_dir == "example_pkg-1.0.0.dist-info/"
            assert whl.metadata_name == "example_pkg-1.0.0.dist-info/METADATA"
            assert whl.wheel_name == "example_pkg-1.0.0.dist-info/WHEEL"
            assert whl.record_name == "example_pkg-1.0.0.dist-info/RECORD"
            entries = whl.distinfolist()
            assert len(entries) == 3
            files = {e.filename: whl.read(e) for e in entries}
        assert b"Name: example_pkg" in files["example_pkg-1.0.0.dist-info/METADATA"]
        assert b"Wheel-Version: 1.0" in files["example_pkg-1.0.0.dist-info/WHEEL"]

    def test_empty_url_raises(self) -> None:
        data, _ = _make_wheel()
        with pytest.raises(ValueError, match=r"does not point to a \.whl"):
            SyncRemoteWheel(MockSyncReader(data))

    def test_not_whl_url_raises(self) -> None:
        data, _ = _make_wheel()
        reader = MockSyncReader(data, url="https://example.com/package.tar.gz")
        with pytest.raises(ValueError, match=r"does not point to a \.whl"):
            SyncRemoteWheel(reader)

    def test_distinfolist_empty(self) -> None:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("module.py", b"x = 1")
        data = buf.getvalue()
        reader = MockSyncReader(data, url="https://example.com/pkg-1.0-py3-none-any.whl")
        with SyncRemoteWheel(reader) as whl:
            assert whl.distinfolist() == []

    def test_with_extra_package_files(self) -> None:
        extra = {
            "example_pkg/__init__.py": b"",
            "example_pkg/core.py": b"def hello(): pass",
        }
        data, filename = _make_wheel(extra_files=extra)
        reader = MockSyncReader(data, url=f"https://example.com/{filename}")
        with SyncRemoteWheel(reader) as whl:
            distinfo_names = {e.filename for e in whl.distinfolist()}
        assert all("dist-info" in n for n in distinfo_names)
        assert not any("__init__" in n for n in distinfo_names)

    def test_subdirectories_included(self) -> None:
        """Files in dist-info subdirectories (licenses, sbom) are included."""
        extra = {
            "example_pkg-1.0.0.dist-info/licenses/LICENSE.txt": b"MIT",
            "example_pkg-1.0.0.dist-info/sbom/pkg.spdx.json": b"{}",
        }
        data, filename = _make_wheel(extra_files=extra)
        reader = MockSyncReader(data, url=f"https://example.com/{filename}")
        with SyncRemoteWheel(reader) as whl:
            files = {e.filename: whl.read(e) for e in whl.distinfolist()}
        assert files["example_pkg-1.0.0.dist-info/licenses/LICENSE.txt"] == b"MIT"
        assert files["example_pkg-1.0.0.dist-info/sbom/pkg.spdx.json"] == b"{}"

    def test_deflated_wheel(self) -> None:
        data, filename = _make_wheel(compression=zipfile.ZIP_DEFLATED)
        reader = MockSyncReader(data, url=f"https://example.com/{filename}")
        with SyncRemoteWheel(reader) as whl:
            metadata = whl.read(f"{whl.dist_info_dir}METADATA")
        assert b"Name: example_pkg" in metadata

    def test_distinfo_not_at_end(self) -> None:
        """Dist-info entries before a large file — falls back to normal read."""
        data, filename = _make_wheel_distinfo_not_at_end()
        reader = MockSyncReader(data, url=f"https://example.com/{filename}")
        with SyncRemoteWheel(reader) as whl:
            metadata = whl.read(f"{whl.dist_info_dir}METADATA")
        assert b"Name: example_pkg" in metadata

    def test_read_directory_returns_empty(self) -> None:
        """Reading a directory entry returns empty bytes."""
        extra = {"example_pkg-1.0.0.dist-info/licenses/": b""}
        data, filename = _make_wheel(extra_files=extra)
        reader = MockSyncReader(data, url=f"https://example.com/{filename}")
        with SyncRemoteWheel(reader) as whl:
            assert whl.read("example_pkg-1.0.0.dist-info/licenses/") == b""

    def test_custom_name_version(self) -> None:
        data, filename = _make_wheel(name="my_pkg", version="3.2.1")
        reader = MockSyncReader(data, url=f"https://example.com/{filename}")
        with SyncRemoteWheel(reader) as whl:
            assert whl.dist_info_dir == "my_pkg-3.2.1.dist-info/"
            metadata = whl.read(f"{whl.dist_info_dir}METADATA")
        assert b"Name: my_pkg" in metadata
        assert b"Version: 3.2.1" in metadata


# ---------------------------------------------------------------------------
# Async tests
# ---------------------------------------------------------------------------


class TestAsyncRemoteWheel:
    async def test_aenter_closes_reader_on_load_failure(self) -> None:
        url = "https://example.com/example_pkg-1.0.0-py3-none-any.whl"
        reader = MockAsyncReader(b"not a zip file", url=url)
        with pytest.raises(BadZipFile):
            async with AsyncRemoteWheel(reader):
                pass
        assert reader.closed

    @pytest.mark.asyncio
    async def test_basic(self) -> None:
        data, filename = _make_wheel()
        reader = MockAsyncReader(data, url=f"https://example.com/{filename}")
        async with AsyncRemoteWheel(reader) as whl:
            assert whl.dist_info_dir == "example_pkg-1.0.0.dist-info/"
            entries = whl.distinfolist()
            assert len(entries) == 3
            metadata = await whl.read(f"{whl.dist_info_dir}METADATA")
        assert b"Name: example_pkg" in metadata

    @pytest.mark.asyncio
    async def test_empty_url_raises(self) -> None:
        data, _ = _make_wheel()
        with pytest.raises(ValueError, match=r"does not point to a \.whl"):
            AsyncRemoteWheel(MockAsyncReader(data))

    @pytest.mark.asyncio
    async def test_distinfolist_empty(self) -> None:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("module.py", b"x = 1")
        data = buf.getvalue()
        reader = MockAsyncReader(data, url="https://example.com/pkg-1.0-py3-none-any.whl")
        async with AsyncRemoteWheel(reader) as whl:
            assert whl.distinfolist() == []

    @pytest.mark.asyncio
    async def test_distinfo_not_at_end(self) -> None:
        data, filename = _make_wheel_distinfo_not_at_end()
        reader = MockAsyncReader(data, url=f"https://example.com/{filename}")
        async with AsyncRemoteWheel(reader) as whl:
            metadata = await whl.read(f"{whl.dist_info_dir}METADATA")
        assert b"Name: example_pkg" in metadata

    @pytest.mark.asyncio
    async def test_read_directory_returns_empty(self) -> None:
        """Reading a directory entry returns empty bytes."""
        extra = {"example_pkg-1.0.0.dist-info/licenses/": b""}
        data, filename = _make_wheel(extra_files=extra)
        reader = MockAsyncReader(data, url=f"https://example.com/{filename}")
        async with AsyncRemoteWheel(reader) as whl:
            assert await whl.read("example_pkg-1.0.0.dist-info/licenses/") == b""

    @pytest.mark.asyncio
    async def test_deflated_wheel(self) -> None:
        data, filename = _make_wheel(compression=zipfile.ZIP_DEFLATED)
        reader = MockAsyncReader(data, url=f"https://example.com/{filename}")
        async with AsyncRemoteWheel(reader) as whl:
            metadata = await whl.read(f"{whl.dist_info_dir}METADATA")
        assert b"Name: example_pkg" in metadata
