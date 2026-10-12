"""Synchronous urllib3-based reader."""

from __future__ import annotations

import logging
import typing
from contextlib import closing

import urllib3

from zipwire._constants import STREAM_CHUNK_SIZE, range_header
from zipwire._errors import RangeRequestUnsupported

if typing.TYPE_CHECKING:
    from collections.abc import Generator

    from zipwire._types import Headers

logger = logging.getLogger(__name__)


class Urllib3Reader:
    """SyncReader implementation using urllib3.PoolManager."""

    def __init__(
        self,
        url: str,
        *,
        pool: urllib3.PoolManager | None = None,
        allow_redirects: bool = True,
    ) -> None:
        self._url = url
        self._owns_pool = pool is None
        self._pool = pool or urllib3.PoolManager()
        self._allow_redirects = allow_redirects

    @property
    def url(self) -> str:
        """The target URL for this reader."""
        return self._url

    def head(self) -> Headers:
        logger.debug("HEAD %s", self._url)
        resp = self._pool.request("HEAD", self._url, redirect=self._allow_redirects)
        if resp.status >= 400:
            raise OSError(f"HEAD request failed with status {resp.status}")
        if resp.headers.get("accept-ranges", "").lower() != "bytes":
            raise RangeRequestUnsupported(
                f"Server does not support range requests for {self._url}"
            )
        return resp.headers

    def read_range(
        self,
        offset: int,
        length: int,
    ) -> tuple[bytes, Headers]:
        logger.debug("GET %s %s (%d bytes)", self._url, range_header(offset, length), length)
        resp = self._pool.request(
            "GET",
            self._url,
            headers={"Range": range_header(offset, length)},
            redirect=self._allow_redirects,
        )
        if resp.status >= 400:
            raise OSError(f"Range request failed with status {resp.status}")
        if resp.status != 206:
            raise RangeRequestUnsupported(
                f"Server does not support range requests for {self._url}"
            )
        return bytes(resp.data), resp.headers

    def stream_range(self, offset: int, length: int) -> Generator[bytes, None, None]:
        logger.debug(
            "GET stream %s %s (%d bytes)", self._url, range_header(offset, length), length
        )
        resp = self._pool.request(
            "GET",
            self._url,
            headers={"Range": range_header(offset, length)},
            preload_content=False,
            redirect=self._allow_redirects,
        )
        try:
            with closing(resp):
                if resp.status >= 400:
                    raise OSError(f"Range request failed with status {resp.status}")
                if resp.status != 206:
                    raise RangeRequestUnsupported(
                        f"Server does not support range requests for {self._url}"
                    )
                yield from resp.stream(STREAM_CHUNK_SIZE)
        finally:
            # Close unread bodies before returning their connections to the pool.
            resp.release_conn()

    def close(self) -> None:
        if self._owns_pool:
            self._pool.clear()
