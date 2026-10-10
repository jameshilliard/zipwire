"""Live S3 tests against public PyTorch wheels (run with ``-m integration``)."""

from __future__ import annotations

import io
import typing
from contextlib import aclosing

import pytest

from tests.conftest import has_aiobotocore, has_httpx2, needs_aiobotocore, needs_httpx2
from zipwire import AsyncRemoteWheel
from zipwire._constants import PREFETCH_THRESHOLD, STREAM_CHUNK_SIZE

if has_aiobotocore and has_httpx2:
    from aiobotocore.config import AioConfig
    from aiobotocore.httpxsession import HttpxSession
    from aiobotocore.session import get_session
    from botocore import UNSIGNED

    from zipwire.backends import AiobotocoreReader

if typing.TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from aiobotocore.client import AioBaseClient

pytestmark = [pytest.mark.integration, needs_aiobotocore, needs_httpx2]

_BUCKET = "pytorch"
_SMALL_KEY = "whl/typing_extensions-4.12.2-py3-none-any.whl"
_LARGE_KEY = "whl/sympy-1.13.1-py3-none-any.whl"


@pytest.fixture
async def public_s3_client() -> AsyncIterator[AioBaseClient]:
    """Use anonymous S3 requests; no AWS credentials or bucket writes needed."""
    async with get_session().create_client(
        "s3",
        region_name="us-east-1",
        endpoint_url="https://s3.us-east-1.amazonaws.com",
        config=AioConfig(
            signature_version=UNSIGNED,
            http_session_cls=HttpxSession,
            connect_timeout=10,
            read_timeout=10,
            retries={"max_attempts": 1},
            max_pool_connections=1,
        ),
    ) as client:
        yield client


@pytest.mark.parametrize("name,version", [("typing_extensions", "4.12.2"), ("sympy", "1.13.1")])
async def test_read_s3_wheel(public_s3_client: AioBaseClient, name: str, version: str) -> None:
    key = f"whl/{name}-{version}-py3-none-any.whl"
    reader = AiobotocoreReader(_BUCKET, key, client=public_s3_client)
    async with AsyncRemoteWheel(reader) as wheel:
        metadata = await wheel.read(wheel.metadata_name)
        assert metadata.startswith(b"Metadata-Version:")
        assert f"\nName: {name}\n".encode() in metadata
        assert f"\nVersion: {version}\n".encode() in metadata

        # Exercise extraction through stream_range for the larger wheel, as
        # well as the small-file prefetch path used for its metadata.
        entry = max(wheel.infolist(), key=lambda info: info.compress_size)
        if name == "sympy":
            assert entry.compress_size > PREFETCH_THRESHOLD
        dest = io.BytesIO()
        await wheel.read_into(entry, dest)
        assert len(dest.getvalue()) == entry.file_size
        assert dest.getvalue() == await wheel.read(entry)


async def test_s3_ranges(public_s3_client: AioBaseClient) -> None:
    async with AiobotocoreReader(_BUCKET, _SMALL_KEY, client=public_s3_client) as reader:
        size = int((await reader.head())["content-length"])
        data, headers = await reader.read_range(0, 4)
        assert data == b"PK\x03\x04"
        assert headers["content-range"] == f"bytes 0-3/{size}"

        # S3 clamps a range extending beyond EOF; it rejects an empty range.
        data, headers = await reader.read_range(size - 22, 100)
        assert len(data) == 22
        assert data.startswith(b"PK\x05\x06")
        assert headers["content-range"] == f"bytes {size - 22}-{size - 1}/{size}"
        assert await reader.read_range(size, 0) == (b"", {"content-length": "0"})


async def test_s3_unfinished_stream(public_s3_client: AioBaseClient) -> None:
    async with AiobotocoreReader(_BUCKET, _LARGE_KEY, client=public_s3_client) as reader:
        size = int((await reader.head())["content-length"])
        assert size > STREAM_CHUNK_SIZE
        async with aclosing(reader.stream_range(0, size)) as stream:
            chunk = await anext(stream)
            assert len(chunk) == STREAM_CHUNK_SIZE
            assert chunk.startswith(b"PK\x03\x04")
            await reader.close()

            # With a single connection available, another request verifies
            # the response was released and the caller's client remains open.
            response = await public_s3_client.head_object(Bucket=_BUCKET, Key=_LARGE_KEY)
            assert response["ContentLength"] == size
