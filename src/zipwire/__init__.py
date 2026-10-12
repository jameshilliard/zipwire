"""Read and extract files from remote ZIP archives over HTTP range requests.

Only the central directory and the requested entries are downloaded,
so large archives can be inspected and partially extracted without
fetching the entire file.

Archive compatibility
---------------------

Compatibility covers supported ZIP metadata and member contents through
a read-only remote archive API. The complete ``zipfile.ZipFile`` interface
is not implemented. Member-range bounds are enforced even where a stdlib
version tolerates an invalid extent; exact exceptions for every malformed
archive are not guaranteed to match across Python versions.

Both APIs read stored, Deflate, Bzip2, and ZIP-LZMA entries. Zstandard
methods 20 and 93 are supported when ``compression.zstd`` is available.
ZIP64, prepended executable data, Unicode Path extra fields, and
traditional ZipCrypto encryption are supported. Pass ``metadata_encoding``
to a generic ZIP constructor for legacy filenames, and ``pwd`` to ``read``
or ``read_into`` for encrypted entries. ``setpassword()`` sets a default
password, and ``comment`` exposes the archive comment as bytes.

The wheel subclasses also support passwords and archive comments; they
always decode filenames as UTF-8 and have no ``metadata_encoding`` argument.
AES encryption,
multi-volume archives, Deflate64, and PPMd remain unsupported.
End-record selection and tolerance of incomplete compressed streams follow
stdlib ``zipfile``. Malformed records, incomplete comments, and null bytes
in filenames remain rejected. Returned data is limited to the declared
uncompressed size and verified against the entry's CRC. Missing optional
compression modules raise ``RuntimeError`` only when that codec is needed.
Compressed members continue decoding beyond the output limit to detect
codec errors, discarding excess output in bounded chunks.
Invalid LZMA properties raise ``lzma.LZMAError``, as in stdlib ``zipfile``.
Premature input exhaustion raises ``TruncatedStream`` (also an ``EOFError``
and ``BadZipFile``) if decoding still needs compressed data.
Archives shorter than the minimum end record are rejected
before any range request.

Archive loading reuses central-directory bytes already present in the
initial tail. Wheel readers also reuse complete cached members. Size
limits are checked before issuing member-specific range requests.
Local header signatures are checked before unsupported entry flags;
filenames and compressed extents are validated before passwords.
Decoded chunks are at most 2 MiB. Total memory also depends on backend
and destination buffering, codec dictionaries, archive metadata, and
the adaptive wheel tail cache. ``max_file_size`` limits returned output,
not total memory or the work spent decoding excess data.
Async reads use AnyIO to yield between bounded decoding steps on asyncio
and Trio, including when all input is already buffered.
``read_into`` retries partial destination writes and yields between retries
in async readers. Writers return a positive byte count or ``None`` when
the whole chunk was consumed; invalid counts raise ``OSError``. Async
writers resolve their awaitable to the same count or ``None``. Each write
is awaited before producing more output, providing backpressure.

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

        # Traditional ZIP passwords are bytes, as in stdlib zipfile.
        # data = rz.read("encrypted.txt", pwd=b"password")

        # Stream a large entry directly to disk
        with open("local.bin", "wb") as fh:
            rz.read_into("large-file.bin", fh)

Asynchronous example
--------------------

::

    import asyncio

    import anyio

    from zipwire import AsyncRemoteZip
    from zipwire.backends import AiohttpReader

    async def main():
        reader = AiohttpReader("https://example.com/archive.zip")
        async with AsyncRemoteZip(reader) as rz:
            print(rz.namelist())
            data = await rz.read("path/inside/archive.txt")

            # Stream a large entry directly to disk
            with open("local.bin", "wb") as fh:
                await rz.read_into("large-file.bin", fh)

            # ... or to an asynchronous writable object
            async with await anyio.open_file("local.bin", "wb") as fh:
                await rz.read_into("large-file.bin", fh)

    asyncio.run(main())

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

``AsyncFileReader`` waits for an active worker operation to finish before
propagating cancellation, so context-manager cleanup can safely close it.

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
    SizeMismatch,
    TruncatedStream,
    UnsupportedCompression,
    UnsupportedFeature,
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
    "SizeMismatch",
    "SyncReader",
    "SyncRemoteWheel",
    "SyncRemoteZip",
    "TruncatedStream",
    "UnsupportedCompression",
    "UnsupportedFeature",
    "Writable",
    "ZipwireError",
]
