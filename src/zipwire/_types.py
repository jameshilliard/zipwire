"""Reader protocols for sync and async IO."""

from __future__ import annotations

import typing

if typing.TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Iterator


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

    def stream_range(self, offset: int, length: int) -> AsyncGenerator[bytes, None]:
        """Stream *length* bytes starting at *offset* as chunks.

        Must return an async generator.  Zipwire closes it with
        :func:`contextlib.aclosing` when extraction stops early, so the
        underlying response is released promptly.

        .. versionchanged:: 0.5.0

           The return type changed from ``AsyncIterator[bytes]`` to
           ``AsyncGenerator[bytes, None]``.  Custom async backends must
           return an async generator so zipwire can close it.
        """
        ...

    async def close(self) -> None:
        """Release any held resources."""
        ...


@typing.runtime_checkable
class Writable(typing.Protocol):
    """Protocol for writable file-like objects (io.BytesIO, io.BufferedWriter, etc.).

    ``write()`` returns the number of bytes accepted, or ``None`` when the
    whole chunk was written.  Buffered and in-memory streams always report
    the count; ``None`` accommodates user-defined writers that signal
    success without one.  A short write is retried with the remaining
    bytes; a count that is not an :class:`int`, or is zero, negative, or
    oversized, raises :exc:`OSError`, because retrying it could loop
    forever.
    """

    def write(self, data: bytes, /) -> int | None: ...


class AsyncWritable(typing.Protocol):
    """Protocol for asynchronous writable objects (anyio.AsyncFile, etc.).

    ``write()`` returns an awaitable that resolves to the number of bytes
    accepted or ``None`` when the whole chunk was written.  Zipwire awaits
    it before producing more output, so a slow destination applies
    backpressure to the transfer.  Short writes are retried and invalid
    byte counts raise :exc:`OSError`, as for :class:`Writable`.

    The protocol is for static typing only.  Synchronous destinations also
    have a ``write`` attribute, so a runtime ``isinstance()`` check cannot
    tell them apart; zipwire awaits the return value when it is awaitable.
    """

    def write(self, data: bytes, /) -> typing.Awaitable[int | None]: ...
