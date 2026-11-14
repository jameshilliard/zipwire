"""Integration tests against real packages on PyPI and Pulp."""

from __future__ import annotations

import io
import typing

import pytest

from tests.conftest import has_aiohttp, has_httpx2, has_requests
from zipwire import AsyncRemoteWheel, AsyncRemoteZip, SyncRemoteWheel, SyncRemoteZip
from zipwire.backends import Urllib3Reader

if typing.TYPE_CHECKING:
    from zipwire.backends import (
        AiohttpReader,
        Httpx2AsyncReader,
        Httpx2SyncReader,
        RequestsReader,
    )

PIP_WHL_URL = (
    "https://files.pythonhosted.org/packages/"
    "5d/95/6b5cb3461ea5673ba0995989746db58eb18b91b54dbf331e72f569540946/"
    "pip-26.1.2-py3-none-any.whl"
)

# Pulp content server (issues redirects, tests redirect-following)
PULP_WHL_URL = (
    "https://packages.redhat.com/api/pulp-content/"
    "public-rhai/rhoai/3.5/cpu-ubi9/"
    "pip-26.0.1-2-py3-none-any.whl"
)

pytestmark = pytest.mark.integration

SYNC_READERS: list[type[Urllib3Reader] | type[Httpx2SyncReader] | type[RequestsReader]] = [
    Urllib3Reader
]
ASYNC_READERS: list[type[Httpx2AsyncReader] | type[AiohttpReader]] = []

if has_httpx2:
    from zipwire.backends import Httpx2AsyncReader, Httpx2SyncReader

    SYNC_READERS.append(Httpx2SyncReader)
    ASYNC_READERS.append(Httpx2AsyncReader)

if has_requests:
    from zipwire.backends import RequestsReader

    SYNC_READERS.append(RequestsReader)

if has_aiohttp:
    from zipwire.backends import AiohttpReader

    ASYNC_READERS.append(AiohttpReader)


def _metadata_path(url: str) -> str:
    """Derive the dist-info METADATA path from a wheel URL."""
    filename = url.rsplit("/", 1)[-1]
    name, version, _ = filename.split("-", 2)
    return f"{name}-{version}.dist-info/METADATA"


def _check_metadata(data: bytes, expected_name: str = "pip") -> None:
    text = data.decode()
    assert text.startswith("Metadata-Version:")
    assert f"Name: {expected_name}" in text


# -- PyPI tests --------------------------------------------------------------


@pytest.mark.parametrize("reader_cls", SYNC_READERS, ids=lambda cls: cls.__name__)
def test_sync_read_metadata(reader_cls) -> None:
    meta = _metadata_path(PIP_WHL_URL)
    reader = reader_cls(PIP_WHL_URL)
    with SyncRemoteZip(reader) as rz:
        assert meta in rz.namelist()
        _check_metadata(rz.read(meta))
        dest = io.BytesIO()
        rz.read_into(meta, dest)
        _check_metadata(dest.getvalue())


@pytest.mark.parametrize("reader_cls", ASYNC_READERS, ids=lambda cls: cls.__name__)
async def test_async_read_metadata(reader_cls) -> None:
    meta = _metadata_path(PIP_WHL_URL)
    reader = reader_cls(PIP_WHL_URL)
    async with AsyncRemoteZip(reader) as rz:
        assert meta in rz.namelist()
        _check_metadata(await rz.read(meta))
        dest = io.BytesIO()
        await rz.read_into(meta, dest)
        _check_metadata(dest.getvalue())


# -- Pulp tests (redirect-following, using wheel subclasses) -----------------


@pytest.mark.parametrize("reader_cls", SYNC_READERS, ids=lambda cls: cls.__name__)
def test_sync_read_metadata_pulp(reader_cls) -> None:
    reader = reader_cls(PULP_WHL_URL)
    with SyncRemoteWheel(reader) as whl:
        assert whl.metadata_name in whl.namelist()
        _check_metadata(whl.read(whl.metadata_name))
        dest = io.BytesIO()
        whl.read_into(whl.metadata_name, dest)
        _check_metadata(dest.getvalue())


@pytest.mark.parametrize("reader_cls", ASYNC_READERS, ids=lambda cls: cls.__name__)
async def test_async_read_metadata_pulp(reader_cls) -> None:
    reader = reader_cls(PULP_WHL_URL)
    async with AsyncRemoteWheel(reader) as whl:
        assert whl.metadata_name in whl.namelist()
        _check_metadata(await whl.read(whl.metadata_name))
        dest = io.BytesIO()
        await whl.read_into(whl.metadata_name, dest)
        _check_metadata(dest.getvalue())
