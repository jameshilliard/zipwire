"""Asynchronous S3 range reader using a caller-owned aiobotocore client."""

from __future__ import annotations

import asyncio
import logging
import typing
from contextlib import AsyncExitStack, asynccontextmanager
from urllib.parse import quote

try:
    from aiobotocore.client import AioBaseClient  # noqa: TC002 - check the optional dependency
except ImportError as exc:
    raise ImportError(
        "AiobotocoreReader requires aiobotocore. Install it with: pip install zipwire[aiobotocore]"
    ) from exc

from zipwire._constants import STREAM_CHUNK_SIZE, range_header
from zipwire._errors import RangeRequestUnsupported

if typing.TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

    from zipwire._types import Headers

logger = logging.getLogger(__name__)

# Parameters shared by HeadObject and GetObject. Bucket, Key, Range, and
# conditional headers belong to the reader so its ranges stay consistent.
_EXTRA_ARGS = frozenset(
    {
        "VersionId",
        "RequestPayer",
        "ExpectedBucketOwner",
        "SSECustomerAlgorithm",
        "SSECustomerKey",
        "SSECustomerKeyMD5",
        "ChecksumMode",
    }
)


class _StreamingBody(typing.Protocol):
    async def read(self, amt: int | None = None) -> bytes: ...

    async def __aenter__(self) -> _StreamingBody: ...

    async def __aexit__(self, *exc: object) -> None: ...


class _ResponseMetadata(typing.TypedDict):
    HTTPStatusCode: int
    HTTPHeaders: dict[str, str]


class _HeadResponse(typing.TypedDict):
    ContentLength: int
    ETag: str
    ResponseMetadata: _ResponseMetadata


class _RangeResponse(_HeadResponse):
    Body: _StreamingBody
    ContentRange: str


class AiobotocoreReader:
    """AsyncReader implementation for an S3 object.

    The caller supplies an open aiobotocore S3 *client* and owns its
    lifecycle. Credentials, endpoint, retries, and HTTP transport are
    configured on that client. This reader owns only its response bodies.

    *bucket* and *key* are passed unchanged to S3. Optional *extra_args*
    are copied and forwarded to both HeadObject and GetObject. Supported
    keys are VersionId, RequestPayer, ExpectedBucketOwner,
    SSECustomerAlgorithm, SSECustomerKey, SSECustomerKeyMD5, and ChecksumMode.

    The first request caches the object's size and ETag. Each range GET
    includes IfMatch to reject an object overwritten during extraction.
    Set VersionId in *extra_args* to read a specific version. SDK errors,
    including a failed condition, propagate to the caller.

    Use one reader per archive/task. A closed reader cannot be reused.
    """

    def __init__(
        self,
        bucket: str,
        key: str,
        *,
        client: AioBaseClient,
        extra_args: Mapping[str, str] | None = None,
    ) -> None:
        self._params = dict(extra_args or {})
        if unsupported := self._params.keys() - _EXTRA_ARGS:
            raise ValueError(f"Unsupported S3 extra_args: {', '.join(sorted(unsupported))}")
        self._params.update(Bucket=bucket, Key=key)
        self._url = f"s3://{bucket}/{quote(key, safe='/')}"
        self._client = client
        self._stat: _HeadResponse | None = None
        self._responses: dict[AsyncExitStack, asyncio.Task[None] | None] = {}
        self._closed = False

    @property
    def url(self) -> str:
        """An S3 URI identifying the object, without credentials."""
        return self._url

    async def _metadata(self) -> _HeadResponse:
        if self._closed:
            raise ValueError("Reader is closed")
        if self._stat is None:
            logger.debug("HEAD %s", self._url)
            response: _HeadResponse = await self._client.head_object(**self._params)
            if response["ContentLength"] < 0 or not response.get("ETag"):
                raise RangeRequestUnsupported(f"S3 did not return size and ETag for {self._url}")
            self._stat = response
        return self._stat

    async def head(self) -> Headers:
        """Return cached object headers, fetching them with HeadObject once."""
        response = await self._metadata()
        return {
            **response["ResponseMetadata"]["HTTPHeaders"],
            "content-length": str(response["ContentLength"]),
            "accept-ranges": "bytes",
            "etag": response["ETag"],
        }

    async def _range_length(self, offset: int, length: int) -> int:
        if offset < 0 or length < 0:
            raise ValueError("Range offset and length must be non-negative")
        stat = await self._metadata()
        size = stat["ContentLength"]
        if offset > size or (offset == size and length):
            raise ValueError("Range starts past the end of the S3 object")
        # ZIP local-header prefetches may extend past EOF. S3 truncates
        # these requests, so validate against the clamped range below.
        return min(length, size - offset)

    @asynccontextmanager
    async def _response(
        self, offset: int, length: int
    ) -> AsyncIterator[tuple[_StreamingBody, Headers]]:
        stat = await self._metadata()
        requested_range = range_header(offset, length)
        logger.debug("GET %s %s (%d bytes)", self._url, requested_range, length)
        response: _RangeResponse = await self._client.get_object(
            **self._params, Range=requested_range, IfMatch=stat["ETag"]
        )
        stack = AsyncExitStack()
        self._responses[stack] = None
        try:
            body = await stack.enter_async_context(response["Body"])
            expected_range = f"bytes {offset}-{offset + length - 1}/{stat['ContentLength']}"
            if (
                response["ResponseMetadata"]["HTTPStatusCode"] != 206
                or response.get("ContentRange") != expected_range
                or response["ContentLength"] != length
            ):
                raise RangeRequestUnsupported(f"S3 returned an unexpected range for {self._url}")
            yield body, response["ResponseMetadata"]["HTTPHeaders"]
        finally:
            await self._close_response(stack)

    async def _close_response(self, stack: AsyncExitStack) -> None:
        """Finish cleanup before releasing ownership or propagating cancellation."""
        if stack not in self._responses:
            return
        task = self._responses[stack]
        if task is None:
            # A stream's finalizer and reader.close() must wait for the same
            # cleanup, even after aclose() has popped the stack's callbacks.
            task = asyncio.create_task(stack.aclose())
            self._responses[stack] = task
        cancelled: asyncio.CancelledError | None = None
        try:
            # Shield protects cleanup, but its caller can still be cancelled
            # repeatedly. Keep waiting so cleanup never outlives this method.
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError as exc:
                    cancelled = exc
            task.result()
        finally:
            self._responses.pop(stack, None)
        if cancelled is not None:
            raise cancelled

    async def read_range(self, offset: int, length: int) -> tuple[bytes, Headers]:
        """Read a range into memory, clamping read-ahead at the object end."""
        length = await self._range_length(offset, length)
        if not length:
            return b"", {"content-length": "0"}
        async with self._response(offset, length) as (body, headers):
            return await body.read(), headers

    async def stream_range(self, offset: int, length: int) -> AsyncIterator[bytes]:
        """Yield a range in bounded chunks, closing its response on exit.

        Close the iterator with ``aclose()`` when stopping early. Closing
        the reader also releases any unfinished response bodies.
        """
        length = await self._range_length(offset, length)
        if not length:
            return
        async with self._response(offset, length) as (body, _):
            while chunk := await body.read(STREAM_CHUNK_SIZE):
                yield chunk

    async def close(self) -> None:
        """Close unfinished responses without closing the supplied client."""
        self._closed = True
        async with AsyncExitStack() as cleanup:
            for stack in self._responses:
                cleanup.push_async_callback(self._close_response, stack)

    async def __aenter__(self) -> AiobotocoreReader:
        if self._closed:
            raise ValueError("Reader is closed")
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()
