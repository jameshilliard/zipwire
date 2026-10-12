"""Reader protocols for sync and async IO."""

from __future__ import annotations

import typing

if typing.TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator


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

    def __getitem__(self, key: str, /) -> str: ...

    def __contains__(self, key: object, /) -> bool: ...

    @typing.overload
    def get(self, key: str, /) -> str | None: ...

    @typing.overload
    def get(self, key: str, default: str | None, /) -> str | None: ...


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
        """Stream *length* bytes starting at *offset* as chunks.

        If the iterator exposes ``close()``, extraction calls it on exit,
        including when decoding or writing fails.
        """
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
        """Stream *length* bytes starting at *offset* as chunks.

        If the iterator exposes ``aclose()``, extraction awaits it on exit,
        including when decoding or writing fails.
        """
        ...

    async def close(self) -> None:
        """Release any held resources."""
        ...


@typing.runtime_checkable
class Writable(typing.Protocol):
    """Synchronous destination accepting bytes from ``read_into``.

    Return the number of bytes accepted, or ``None`` to indicate that the
    whole chunk was consumed. Partial writes are retried; zero, negative,
    or oversized counts raise ``OSError``. Destinations must raise
    ``BlockingIOError`` if they cannot currently accept data.

    The caller owns the destination and is responsible for flushing and
    closing it.
    """

    def write(self, data: bytes, /) -> int | None: ...
