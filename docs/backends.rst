Backends
========

zipwire uses pluggable backends to fetch byte ranges from HTTP servers,
S3 objects, and local files. Each backend is a thin wrapper that
implements the :class:`~zipwire.SyncReader` or :class:`~zipwire.AsyncReader`
protocol.

Available backends
------------------

.. list-table::
   :header-rows: 1
   :widths: 15 25 10 10 20

   * - Library
     - Class
     - Mode
     - HTTP
     - Install extra
   * - urllib3
     - :class:`~zipwire.backends.Urllib3Reader`
     - sync
     - 1.1
     - *(included)*
   * - httpx2
     - :class:`~zipwire.backends.Httpx2SyncReader`
     - sync
     - 1.1, 2
     - ``httpx2``
   * - httpx2
     - :class:`~zipwire.backends.Httpx2AsyncReader`
     - async
     - 1.1, 2
     - ``httpx2``
   * - requests
     - :class:`~zipwire.backends.RequestsReader`
     - sync
     - 1.1
     - ``requests``
   * - aiohttp
     - :class:`~zipwire.backends.AiohttpReader`
     - async
     - 1.1
     - ``aiohttp``
   * - aiobotocore
     - :class:`~zipwire.backends.AiobotocoreReader`
     - async
     - S3 API
     - ``aiobotocore``
   * - local files
     - :class:`~zipwire.backends.FileReader`
     - sync
     - --
     - *(included)*
   * - local files
     - :class:`~zipwire.backends.AsyncFileReader`
     - async
     - --
     - *(included)*

Choosing a backend
------------------

- **Default / no extra dependency** - use :class:`~zipwire.backends.Urllib3Reader`.
  urllib3 is already a dependency of zipwire.
- **Async with aiohttp** - use :class:`~zipwire.backends.AiohttpReader`.
  Best choice if your project already depends on aiohttp.
- **Sync + async from one library** - install ``httpx2`` and use
  :class:`~zipwire.backends.Httpx2SyncReader` or
  :class:`~zipwire.backends.Httpx2AsyncReader`.  The ``httpx2`` extra includes
  HTTP/2 support via the ``h2`` package.  When ``h2`` is available, HTTP/2 is
  enabled automatically; pass ``http2=False`` to force HTTP/1.1 or
  ``http2=True`` to require it.
- **Requests integration** - use :class:`~zipwire.backends.RequestsReader`
  if your project already uses ``requests`` and you want to share sessions,
  authentication, or retry configuration.
- **Local archives** - use :class:`~zipwire.backends.FileReader` or
  :class:`~zipwire.backends.AsyncFileReader` to read a ZIP or wheel that
  already lives on the local filesystem.  No extra dependency is required.
- **S3 archives** - use :class:`~zipwire.backends.AiobotocoreReader` with an
  existing async S3 client to read private buckets or S3-compatible services.

Reading S3 objects
------------------

Install ``zipwire[aiobotocore]`` and pass an open aiobotocore S3 client to
:class:`~zipwire.backends.AiobotocoreReader`. The bucket and key are passed
unchanged to the SDK. The client controls authentication, endpoints,
retries, and the HTTP transport. Zipwire does not create or close the client.

.. code-block:: python

   from aiobotocore.session import get_session

   from zipwire import AsyncRemoteZip
   from zipwire.backends import AiobotocoreReader

   async with get_session().create_client("s3", region_name="us-east-1") as client:
       reader = AiobotocoreReader("my-bucket", "archives/data.zip", client=client)
       async with AsyncRemoteZip(reader) as rz:
           data = await rz.read("file.txt")

The reader caches ``HeadObject`` metadata and sends ``IfMatch`` with every
range GET. An overwrite fails with the SDK's precondition error instead of
combining bytes from different objects. To select a particular version, pass
``extra_args={"VersionId": "..."}``. Other supported extra arguments are
``RequestPayer``, ``ExpectedBucketOwner``, ``SSECustomerAlgorithm``,
``SSECustomerKey``, ``SSECustomerKeyMD5``, and ``ChecksumMode``. They are sent
to both ``HeadObject`` and ``GetObject``. SDK errors propagate unchanged.

Range responses must have status 206 and matching ``ContentRange`` and
``ContentLength``. Read-ahead is clamped at the object's end; zero-length
ranges do not issue a GET. ``stream_range`` reads in bounded chunks, while
``read_range`` buffers its requested range in memory.

Each response body is managed with an async context manager. Closing the
reader also closes unfinished responses, leaving the supplied client open.
Response cleanup finishes before cancellation propagates, including repeated
asyncio task cancellation and timeouts.
When using ``stream_range`` directly, wrap it with ``contextlib.aclosing``
if iteration may stop early. The reader itself supports ``async with`` and
cannot be reused after closing. Use one reader per archive/task.

To use aiobotocore's HTTPX2 transport, install ``aiobotocore[httpx2]`` and
pass ``AioConfig(http_session_cls=HttpxSession)`` to ``create_client``;
import these classes from ``aiobotocore.config`` and
``aiobotocore.httpxsession`` respectively. The reader works with either
aiobotocore transport.

Reading local files
-------------------

:class:`~zipwire.backends.FileReader` and
:class:`~zipwire.backends.AsyncFileReader` back the same
:class:`~zipwire.SyncRemoteZip` / :class:`~zipwire.AsyncRemoteZip` API with
local file IO, so identical code opens a local or a remote archive.  Instead
of an HTTP round-trip, :meth:`~zipwire.backends.FileReader.head` synthesises
``Content-Length`` / ``Accept-Ranges`` from ``os.fstat`` on the open handle
(the size is cached, assuming the file does not change), and range reads
seek into a lazily-opened file handle.  A missing path raises
:exc:`FileNotFoundError`, mirroring how a network 404 surfaces as
:exc:`OSError`.

Both accept either a path (``str`` / :class:`os.PathLike`) or a ``file://``
URI via :meth:`~zipwire.backends.FileReader.from_uri` (percent-decoded, with
an empty or ``localhost`` host).

.. code-block:: python

   from zipwire import SyncRemoteZip
   from zipwire.backends import FileReader

   with SyncRemoteZip(FileReader("/path/to/archive.zip")) as rz:
       data = rz.read("file.txt")

   reader = FileReader.from_uri("file:///path/to/archive.zip")

Regular files do not support true non-blocking IO, so
:class:`~zipwire.backends.AsyncFileReader` offloads every blocking call to a
worker thread via :func:`asyncio.to_thread` -- the same approach libraries
such as ``aiofiles`` use internally, without the extra dependency.

.. code-block:: python

   import asyncio

   from zipwire import AsyncRemoteZip
   from zipwire.backends import AsyncFileReader


   async def main():
       async with AsyncRemoteZip(AsyncFileReader("/path/to/archive.zip")) as rz:
           print(await rz.read("file.txt"))


   asyncio.run(main())

Passing an existing client
--------------------------

HTTP backend constructors accept an optional pre-configured client or
session; :class:`~zipwire.backends.AiobotocoreReader` requires an S3 client.
When you pass one, zipwire will **not** close it - you remain responsible
for its lifecycle.

.. code-block:: python

   import aiohttp

   from zipwire import AsyncRemoteZip
   from zipwire.backends import AiohttpReader

   async with aiohttp.ClientSession() as session:
       reader = AiohttpReader(
           "https://archive.example/data.zip", session=session
       )
       async with AsyncRemoteZip(reader) as rz:
           data = await rz.read("file.txt")

Writing a custom backend
-------------------------

Implement the :class:`~zipwire.SyncReader` or :class:`~zipwire.AsyncReader`
protocol and pass an instance to :class:`~zipwire.SyncRemoteZip` or
:class:`~zipwire.AsyncRemoteZip`:

.. code-block:: python

   from collections.abc import Iterator

   from zipwire import Headers, SyncReader, SyncRemoteZip


   class MyReader:
       """Minimal SyncReader implementation."""

       def __init__(self, url: str) -> None:
           self._url = url

       def head(self) -> Headers:
           ...  # HEAD request, check Accept-Ranges, return headers

       def read_range(
           self,
           offset: int,
           length: int,
       ) -> tuple[bytes, Headers]:
           ...  # GET with Range header, return (data, response_headers)

       def stream_range(self, offset: int, length: int) -> Iterator[bytes]:
           ...  # GET with Range header, yield chunks

       def close(self) -> None:
           ...  # release resources

   assert isinstance(MyReader("..."), SyncReader)  # runtime-checkable

See the :class:`~zipwire.SyncReader` and :class:`~zipwire.AsyncReader`
protocol definitions for the full method signatures.

Async readers can return any asynchronous iterator from ``stream_range``.
If it provides an async ``aclose()`` method,
:meth:`~zipwire.AsyncRemoteZip.read_into` awaits it before returning or
propagating an extraction error. An async generator can release per-stream
resources in a ``finally`` block. Iterators without ``aclose()`` remain
supported; the reader remains responsible for any resources they retain.

Why only absolute byte ranges?
-------------------------------

HTTP range requests support two forms: absolute ranges
(``bytes=<start>-<end>``) and suffix ranges (``bytes=-<N>``).  Suffix
ranges request the last *N* bytes without requiring the client to know
the total file size.  While RFC 7233 defines both forms, support varies
across CDNs and cloud storage services:

.. list-table::
   :header-rows: 1
   :widths: 25 15 40

   * - Service
     - Suffix ranges
     - Notes
   * - AWS S3
     - supported
     - Full RFC 7233 compliance.
   * - Google Cloud Storage
     - supported
     - Confirmed by GCS team.
   * - Azure Blob Storage
     - **not supported**
     - Returns 200 with full object instead of 206.
   * - Amazon CloudFront
     - **not supported**
     - Returns 200 with full object instead of 206.
   * - Cloudflare R2
     - supported
     - Via S3-compatible and Workers API.
   * - Fastly
     - varies
     - Some Fastly-fronted origins (e.g. PyPI) return 501.
   * - Cloudflare CDN
     - uncertain
     - Undocumented; may pass through to origin.
   * - Akamai
     - likely supported
     - General range support confirmed; suffix ranges undocumented.

Because suffix ranges silently break on several major platforms (returning
the full object or an error), zipwire uses only absolute byte ranges.  On
first access it sends a HEAD request to learn the file size, then
computes absolute offsets for all subsequent range requests.  This adds
one round-trip compared to a suffix-range approach but works reliably
everywhere.
