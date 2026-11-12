"""Write decoded chunks completely, retrying short writes."""

from __future__ import annotations

import typing
from inspect import isawaitable

if typing.TYPE_CHECKING:
    from collections.abc import Generator, Iterable

    from zipwire._types import AsyncWritable, Writable


def _write_steps(chunks: Iterable[bytes]) -> Generator[bytes, int | None, None]:
    """Drive short-write retries, shared by the sync and async paths.

    Yields the next part to write and receives the result of
    ``dest.write(part)`` back through ``send()``, advancing by the
    accepted amount:

    - ``None``: the whole part was accepted.
    - an :class:`int` with ``0 < written <= len(part)``: the number of
      bytes accepted.
    - anything else, including a :class:`bool` success flag: raises
      :exc:`OSError`.
    """
    for chunk in chunks:
        offset = 0
        write_size = len(chunk)
        while offset < len(chunk):
            part = chunk[offset : offset + write_size]
            written = yield part
            if written is None:
                written = len(part)
            # type() rather than isinstance(): bool is an int subclass,
            # and a True/False success flag must not be misread as a count.
            elif type(written) is not int or not 0 < written <= len(part):
                raise OSError("write() returned an invalid byte count")
            offset += written
            write_size = written


def write_all(dest: Writable, chunks: Iterable[bytes]) -> None:
    """Write every chunk, retrying partial writes."""
    steps = _write_steps(chunks)
    try:
        part = next(steps)
    except StopIteration:
        return
    while True:
        result = dest.write(part)
        try:
            part = steps.send(result)
        except StopIteration:
            return


async def awrite_all(dest: Writable | AsyncWritable, chunks: Iterable[bytes]) -> None:
    """Write every chunk, retrying partial writes and awaiting awaitables.

    Awaiting the write result yields control on any event loop, so this
    coroutine never calls loop-specific APIs such as ``asyncio.sleep(0)``
    and works under asyncio, trio, or without an event loop at all.
    """
    steps = _write_steps(chunks)
    try:
        part = next(steps)
    except StopIteration:
        return
    while True:
        result = dest.write(part)
        # Sync file writes return the byte count; skip the costlier
        # awaitable probe for the common case.
        if type(result) is not int and isawaitable(result):
            result = await result
        try:
            part = steps.send(result)
        except StopIteration:
            return
