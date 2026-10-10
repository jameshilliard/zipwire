"""Read and extract files from remote ZIP archives over HTTP range requests.

Only the central directory and the requested entries are downloaded,
so large archives can be inspected and partially extracted without
fetching the entire file.

Synchronous example
-------------------

::

    from zipwire import SyncRemoteZip
    from zipwire.backends import Urllib3Reader

    reader = Urllib3Reader("https://example.com/archive.zip")
    with SyncRemoteZip(reader) as rz:
        for info in rz.infolist():
            print(f"{info.filename}  {info.file_size} bytes")
        data = rz.read("path/inside/archive.txt")

        # Stream a large entry directly to disk
        with open("local.bin", "wb") as fh:
            rz.read_into("large-file.bin", fh)

Asynchronous example
--------------------

::

    import asyncio
    import anyio
    from zipwire import AsyncRemoteZip
    from zipwire.backends import Httpx2AsyncReader

    async def main():
        reader = Httpx2AsyncReader("https://example.com/archive.zip")
        async with AsyncRemoteZip(reader) as rz:
            print(rz.namelist())
            data = await rz.read("path/inside/archive.txt")

            # Await writes to an async destination (AnyIO installed separately)
            async with await anyio.open_file("local.bin", "wb") as fh:
                await rz.read_into("large-file.bin", fh)

    asyncio.run(main())

``AsyncRemoteZip.read_into`` accepts both ``Writable`` and ``AsyncWritable``
destinations. Async writes are awaited before extraction continues. The
caller owns the destination and handles flushing, closing, or committing it.

Backends
--------

All backends live in :mod:`zipwire.backends` and are lazily imported.

Synchronous:
  - ``Urllib3Reader`` -- default, uses *urllib3*
  - ``RequestsReader`` -- uses *requests* (``pip install zipwire[requests]``)
  - ``Httpx2SyncReader`` -- uses *httpx2*, supports HTTP/2
    (``pip install zipwire[httpx2]``)

Asynchronous:
  - ``AiohttpReader`` -- uses *aiohttp* (``pip install zipwire[aiohttp]``)
  - ``Httpx2AsyncReader`` -- uses *httpx2*, supports HTTP/2
    (``pip install zipwire[httpx2]``)

Local files:
  - ``FileReader`` / ``AsyncFileReader`` -- read an archive from the local
    filesystem through the same API (no extra dependency).  Useful for
    testing or for handling ``file://`` URIs and local paths uniformly
    with HTTP URLs.

::

    from zipwire import SyncRemoteZip
    from zipwire.backends import FileReader

    with SyncRemoteZip(FileReader("/path/to/archive.zip")) as rz:
        data = rz.read("path/inside/archive.txt")

    # From a file:// URI
    reader = FileReader.from_uri("file:///path/to/archive.zip")

Wheel subclasses
----------------

``SyncRemoteWheel`` and ``AsyncRemoteWheel`` extend the base classes
for Python wheels.  They parse the wheel URL to derive the
``.dist-info`` directory name, fetch an adaptive tail (at least
128 KiB, scaling with archive size) to cover dist-info entries at
the end of the archive, and serve matching reads from the tail
buffer without extra HTTP requests.

::

    from zipwire import SyncRemoteWheel
    from zipwire.backends import Urllib3Reader

    url = "https://files.pythonhosted.org/.../requests-2.32.3-py3-none-any.whl"
    reader = Urllib3Reader(url)
    with SyncRemoteWheel(reader) as whl:
        for entry in whl.distinfolist():
            print(entry.filename, whl.read(entry))
"""

from __future__ import annotations

from zipwire._async import AsyncRemoteZip
from zipwire._constants import CompressionMethod
from zipwire._errors import (
    BadZipFile,
    CRCMismatch,
    FileNotFoundInZip,
    FileTooLarge,
    RangeRequestUnsupported,
    UnsupportedCompression,
    ZipwireError,
)
from zipwire._parser import EOCDInfo
from zipwire._sync import SyncRemoteZip
from zipwire._types import AsyncReader, AsyncWritable, Headers, SyncReader, Writable
from zipwire._wheel import AsyncRemoteWheel, SyncRemoteWheel
from zipwire._zipinfo import RemoteZipInfo

__all__ = [
    "AsyncReader",
    "AsyncRemoteWheel",
    "AsyncRemoteZip",
    "AsyncWritable",
    "BadZipFile",
    "CRCMismatch",
    "CompressionMethod",
    "EOCDInfo",
    "FileNotFoundInZip",
    "FileTooLarge",
    "Headers",
    "RangeRequestUnsupported",
    "RemoteZipInfo",
    "SyncReader",
    "SyncRemoteWheel",
    "SyncRemoteZip",
    "UnsupportedCompression",
    "Writable",
    "ZipwireError",
]
