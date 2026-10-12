"""Wheel dist-info extraction via RemoteZip subclasses.

The `wheel specification`_ recommends that archivers place the
``.dist-info`` directory at the end of the ZIP archive so that
metadata can be accessed without reading the entire file.

``SyncRemoteWheel`` and ``AsyncRemoteWheel`` exploit this by fetching
an enlarged tail in the initial request.  Entries that fall within the
tail are served from the buffer without extra HTTP requests.

The tail size is adaptive: ``max(128 KiB, file_size // 256)``.  A
fixed 128 KiB tail covers 93% of typical PyPI wheels, but large
wheels with many files can push METADATA further from the end.
RECORD is written last (it contains checksums of all other files)
and can be hundreds of KiB for wheels with many entries.  Additional
dist-info files (licenses, SBOMs, build metadata) add more distance.
The adaptive size scales to ~0.4% of the archive, covering even
multi-GB wheels like PyTorch (~5.8 MB tail for a 1.5 GB wheel).

.. _wheel specification:
    https://packaging.python.org/en/latest/specifications/binary-distribution-format/#recommended-archiver-features
"""

from __future__ import annotations

import typing
from urllib.parse import unquote, urlparse

from zipwire._async import AsyncRemoteZip
from zipwire._base import RemoteZipBase
from zipwire._parser import parse_local_file_header
from zipwire._sync import SyncRemoteZip

if typing.TYPE_CHECKING:
    import zipfile

    from zipwire._types import AsyncReader, SyncReader
    from zipwire._zipinfo import RemoteZipInfo

# Minimum tail fetch size.  The actual tail is max(_MIN_TAIL_SIZE,
# file_size // 256), scaling to ~0.4% of the archive for large wheels.
_MIN_TAIL_SIZE = 128 * 1024


def _dist_info_from_url(url: str) -> str:
    """Derive the dist-info prefix from a wheel URL.

    Parses the URL path to extract the wheel filename, then derives
    ``{name}-{version}.dist-info/`` per PEP 427.  Strict wheel
    filename validation is not required here -- we only need name and
    version, and any filename that pip/PyPI already accepted is good
    enough.

    Raises:
        ValueError: If the URL path does not end with ``.whl`` or the
            filename does not have the expected number of parts.
    """
    path = unquote(urlparse(url).path)
    filename = path.rsplit("/", 1)[-1]
    if not filename.lower().endswith(".whl"):
        raise ValueError(f"URL does not point to a .whl file: {filename!r}")
    # PEP 427: {name}-{version}(-{build})?-{python}-{abi}-{platform}.whl
    # That gives 5 parts without a build tag, 6 with one.
    stem = filename[: -len(".whl")]
    parts = stem.split("-")
    if len(parts) not in (5, 6):
        raise ValueError(f"Cannot parse wheel filename: {filename!r}")
    return f"{parts[0]}-{parts[1]}.dist-info/"


class _RemoteWheelMixin(RemoteZipBase):
    """Shared state and accessors for wheel subclasses."""

    _dist_info_dir: str
    _tail_start: int
    _tail_data: bytes

    def _init_wheel(self, url: str) -> None:
        """Parse the wheel URL and initialise tail fields."""
        self._dist_info_dir = _dist_info_from_url(url)
        self._tail_start = 0
        self._tail_data = b""

    @property
    def dist_info_dir(self) -> str:
        """The dist-info directory name including trailing slash."""
        return self._dist_info_dir

    @property
    def metadata_name(self) -> str:
        """Archive path of the ``METADATA`` file."""
        return f"{self._dist_info_dir}METADATA"

    @property
    def wheel_name(self) -> str:
        """Archive path of the ``WHEEL`` file."""
        return f"{self._dist_info_dir}WHEEL"

    @property
    def record_name(self) -> str:
        """Archive path of the ``RECORD`` file."""
        return f"{self._dist_info_dir}RECORD"

    def _tail_read_size(self, file_size: int) -> int:
        # At least 128 KiB (covers 93% of PyPI wheels), scaling to
        # ~0.4% of the archive for large wheels so that dist-info at
        # the end of multi-GB wheels (e.g. PyTorch, vLLM) is covered.
        return max(_MIN_TAIL_SIZE, file_size // 256)

    def _clear(self) -> None:
        super()._clear()
        self._tail_start = 0
        self._tail_data = b""

    def _resolve_from_tail(
        self,
        info: zipfile.ZipInfo,
    ) -> tuple[zipfile.ZipInfo, int, bytes] | None:
        """Try to resolve an entry's compressed data from the tail buffer.

        Returns ``(info, data_offset, compressed)`` if the entry fits
        entirely within the tail, otherwise ``None``.
        """
        tail_start = self._tail_start
        tail_end = tail_start + len(self._tail_data)
        if info.header_offset < tail_start:
            return None
        rel = info.header_offset - tail_start
        header = parse_local_file_header(self._tail_data, offset=rel)
        data_offset = self._validate_local_header(info, self._tail_data, header, offset=rel)
        if data_offset + info.compress_size > tail_end:
            return None
        data_start = data_offset - tail_start
        compressed = self._tail_data[data_start : data_start + info.compress_size]
        return info, data_offset, compressed

    def distinfolist(self) -> list[RemoteZipInfo]:
        """Return file entries in the dist-info directory.

        Returns only files, not directory entries.
        """
        prefix = self._dist_info_dir
        return [e for e in self.infolist() if e.filename.startswith(prefix) and not e.is_dir()]


class SyncRemoteWheel(_RemoteWheelMixin, SyncRemoteZip):
    """Read files from a remote wheel with an enlarged tail fetch.

    Parses the wheel URL to derive the ``.dist-info`` directory name
    and fetches a larger tail so that dist-info entries at the end of
    the archive are served from the buffer without extra HTTP requests.

    Must be used as a context manager.  Not thread-safe.

    Usage::

        with SyncRemoteWheel(reader) as whl:
            print(whl.dist_info_dir)
            for entry in whl.distinfolist():
                print(entry.filename, whl.read(entry))
    """

    def __init__(self, reader: SyncReader, *, metadata_encoding: str | None = None) -> None:
        super().__init__(reader, metadata_encoding=metadata_encoding)
        self._init_wheel(reader.url)

    def __enter__(self) -> SyncRemoteWheel:
        try:
            self._tail_start, self._tail_data = self._ensure_loaded()
        except BaseException:
            self.close()
            raise
        return self

    def _resolve_entry(
        self,
        info: zipfile.ZipInfo,
    ) -> tuple[zipfile.ZipInfo, int, bytes | None]:
        result = self._resolve_from_tail(info)
        if result is not None:
            return result
        return super()._resolve_entry(info)


class AsyncRemoteWheel(_RemoteWheelMixin, AsyncRemoteZip):
    """Read files from a remote wheel with an enlarged tail fetch (async).

    Parses the wheel URL to derive the ``.dist-info`` directory name
    and fetches a larger tail so that dist-info entries at the end of
    the archive are served from the buffer without extra HTTP requests.

    Must be used as an async context manager.  Not thread-safe.

    Usage::

        async with AsyncRemoteWheel(reader) as whl:
            print(whl.dist_info_dir)
            for entry in whl.distinfolist():
                print(entry.filename, await whl.read(entry))
    """

    def __init__(self, reader: AsyncReader, *, metadata_encoding: str | None = None) -> None:
        super().__init__(reader, metadata_encoding=metadata_encoding)
        self._init_wheel(reader.url)

    async def __aenter__(self) -> AsyncRemoteWheel:
        try:
            self._tail_start, self._tail_data = await self._ensure_loaded()
        except BaseException:
            await self.close()
            raise
        return self

    async def _resolve_entry(
        self,
        info: zipfile.ZipInfo,
    ) -> tuple[zipfile.ZipInfo, int, bytes | None]:
        result = self._resolve_from_tail(info)
        if result is not None:
            return result
        return await super()._resolve_entry(info)
