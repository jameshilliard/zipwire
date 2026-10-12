"""Write decoded chunks completely, with optional caller-provided checkpoints."""

from __future__ import annotations

import typing
from inspect import isawaitable

if typing.TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable, Iterator

    from zipwire._types import AsyncWritable, Writable


def _write_count(written: object, length: int) -> int:
    if written is None:
        return length
    if type(written) is not int or not 0 < written <= length:
        raise OSError("write() returned an invalid byte count")
    return written


def _write_steps(dest: Writable, chunks: Iterable[bytes]) -> Iterator[None]:
    for chunk in chunks:
        if not chunk:
            yield
            continue
        offset = 0
        write_size = len(chunk)
        while offset < len(chunk):
            part = chunk[offset : offset + write_size]
            written = _write_count(dest.write(part), len(part))
            offset += written
            # Retry short writes with smaller slices to avoid repeatedly
            # copying a large remainder when only a few bytes are accepted.
            write_size = written
            yield


def write_all(dest: Writable, chunks: Iterable[bytes]) -> None:
    """Write every chunk, retrying partial writes."""
    for _ in _write_steps(dest, chunks):
        pass


async def awrite_all(
    dest: Writable | AsyncWritable,
    chunks: Iterable[bytes],
    *,
    checkpoint: Callable[[], Awaitable[None]] | None = None,
) -> None:
    """Write every chunk, optionally yielding between decoding and write steps.

    Without a checkpoint callback this helper is event-loop independent.
    Callers that need cooperative scheduling can supply their loop's checkpoint.
    """
    for chunk in chunks:
        if not chunk:
            if checkpoint is not None:
                await checkpoint()
            continue
        offset = 0
        write_size = len(chunk)
        while offset < len(chunk):
            part = chunk[offset : offset + write_size]
            result = dest.write(part)
            if type(result) is not int and isawaitable(result):
                result = await result
            written = _write_count(result, len(part))
            offset += written
            write_size = written
            if checkpoint is not None:
                await checkpoint()
