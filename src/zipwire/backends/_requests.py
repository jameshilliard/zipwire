"""Synchronous requests-based reader."""

from __future__ import annotations

import logging
import typing

try:
    import requests
except ImportError as exc:
    raise ImportError(
        "RequestsReader requires requests. Install it with: pip install zipwire[requests]"
    ) from exc

from zipwire._constants import STREAM_CHUNK_SIZE, range_header
from zipwire._errors import RangeRequestUnsupported

if typing.TYPE_CHECKING:
    from collections.abc import Generator

    from zipwire._types import Headers

logger = logging.getLogger(__name__)


class RequestsReader:
    """SyncReader implementation using requests.Session."""

    def __init__(
        self,
        url: str,
        *,
        session: requests.Session | None = None,
        allow_redirects: bool = True,
    ) -> None:
        self._url = url
        self._owns_session = session is None
        self._session = session or requests.Session()
        self._allow_redirects = allow_redirects

    @property
    def url(self) -> str:
        """The target URL for this reader."""
        return self._url

    def head(self) -> Headers:
        logger.debug("HEAD %s", self._url)
        resp = self._session.head(self._url, allow_redirects=self._allow_redirects)
        resp.raise_for_status()
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
        resp = self._session.get(
            self._url,
            headers={"Range": range_header(offset, length)},
            allow_redirects=self._allow_redirects,
        )
        resp.raise_for_status()
        if resp.status_code != 206:
            raise RangeRequestUnsupported(
                f"Server does not support range requests for {self._url}"
            )
        return resp.content, resp.headers

    def stream_range(self, offset: int, length: int) -> Generator[bytes, None, None]:
        logger.debug(
            "GET stream %s %s (%d bytes)", self._url, range_header(offset, length), length
        )
        with self._session.get(
            self._url,
            headers={"Range": range_header(offset, length)},
            stream=True,
            allow_redirects=self._allow_redirects,
        ) as resp:
            resp.raise_for_status()
            if resp.status_code != 206:
                raise RangeRequestUnsupported(
                    f"Server does not support range requests for {self._url}"
                )
            yield from resp.iter_content(chunk_size=STREAM_CHUNK_SIZE)

    def close(self) -> None:
        if self._owns_session:
            self._session.close()
