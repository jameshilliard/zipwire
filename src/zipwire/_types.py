"""Reader protocols for sync and async IO."""

from __future__ import annotations

import typing

if typing.TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Iterator


@typing.runtime_checkable
class Headers(typing.Protocol):
    """HTTP response headers returned by :meth:`head` and :meth:`read_range`.

    Implementations must support case-insensitive lookup or use
    lower-case keys so that ``headers["content-length"]`` always works.

    All standard HTTP header mapping types satisfy this protocol,
    including ``urllib3.HTTPHeaderDict``, ``requests.structures.CaseInsensitiveDict``,
    ``httpx2.Headers``, ``aiohttp.CIMultiDictProxy``, and plain ``dict``
    with lower-case keys.
    """

    def __getitem__(self, key: str) -> str: ...

    def __contains__(self, key: object) -> bool: ...

    def get(self, key: str, default: str | None = None) -> str | None: ...


@typing.runtime_checkable
class SyncReader(typing.Protocol):
    """Protocol for synchronous HTTP range-request readers."""

    @property
    def url(self) -> str:
        """The target URL for this reader."""
        ...

    def read_range(
        self,
        offset: int,
        length: int,
    ) -> tuple[bytes, Headers]:
        """Read *length* bytes starting at absolute byte position *offset*.

        Sends ``Range: bytes=<offset>-<offset+length-1>``.

        Returns the response body and the HTTP response headers.
        """
        ...

    def head(self) -> Headers:
        """Send a HEAD request and return the response headers.

        Raises:
            RangeRequestUnsupported: If the server does not advertise
                ``Accept-Ranges: bytes``.
        """
        ...

    def stream_range(self, offset: int, length: int) -> Iterator[bytes]:
        """Stream *length* bytes starting at *offset* as chunks."""
        ...

    def close(self) -> None:
        """Release any held resources."""
        ...


@typing.runtime_checkable
class AsyncReader(typing.Protocol):
    """Protocol for asynchronous HTTP range-request readers."""

    @property
    def url(self) -> str:
        """The target URL for this reader."""
        ...

    async def read_range(
        self,
        offset: int,
        length: int,
    ) -> tuple[bytes, Headers]:
        """Read *length* bytes starting at absolute byte position *offset*.

        Sends ``Range: bytes=<offset>-<offset+length-1>``.

        Returns the response body and the HTTP response headers.
        """
        ...

    async def head(self) -> Headers:
        """Send a HEAD request and return the response headers.

        Raises:
            RangeRequestUnsupported: If the server does not advertise
                ``Accept-Ranges: bytes``.
        """
        ...

    def stream_range(self, offset: int, length: int) -> AsyncIterator[bytes]:
        """Stream *length* bytes starting at *offset* as chunks."""
        ...

    async def close(self) -> None:
        """Release any held resources."""
        ...


@typing.runtime_checkable
class Writable(typing.Protocol):
    """Synchronous destination that writes each entire chunk or raises.

    The caller owns the destination and is responsible for flushing and
    closing it. The return value of :meth:`write` is ignored.
    """

    def write(self, data: bytes, /) -> object: ...


@typing.runtime_checkable
class AsyncWritable(typing.Protocol):
    """Asynchronous destination that writes each entire chunk or raises.

    Each write is awaited before extraction continues. The caller owns the
    destination and is responsible for flushing, closing, or committing it.
    The awaited return value is ignored.
    """

    def write(self, data: bytes, /) -> Awaitable[object]: ...
