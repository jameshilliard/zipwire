Quick Start
===========

Synchronous usage
-----------------

List files in a remote ZIP
^^^^^^^^^^^^^^^^^^^^^^^^^^

.. code-block:: python

   from zipwire import SyncRemoteZip
   from zipwire.backends import Urllib3Reader

   reader = Urllib3Reader("https://archive.example/data.zip")
   with SyncRemoteZip(reader) as rz:
       for info in rz.infolist():
           print(f"{info.filename}  {info.file_size} bytes")

Read a single file into memory
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

.. code-block:: python

   from zipwire import SyncRemoteZip
   from zipwire.backends import Httpx2SyncReader

   reader = Httpx2SyncReader("https://archive.example/data.zip")
   with SyncRemoteZip(reader) as rz:
       data = rz.read("path/to/file.txt")

Stream a file to disk
^^^^^^^^^^^^^^^^^^^^^

:meth:`~zipwire.SyncRemoteZip.read_into` streams the compressed payload in
chunks and decompresses on the fly, keeping peak memory low even for large
entries:

.. code-block:: python

   from zipwire import SyncRemoteZip
   from zipwire.backends import RequestsReader

   reader = RequestsReader("https://archive.example/large.zip")
   with SyncRemoteZip(reader) as rz:
       with open("output.bin", "wb") as f:
           rz.read_into("big-file.bin", f)

Custom destinations should return the number of bytes written, or
``None`` if the whole chunk was consumed. Short writes are retried;
invalid byte counts raise ``OSError``. Destinations that would block
must raise ``BlockingIOError``.

Read a local archive
^^^^^^^^^^^^^^^^^^^^

:class:`~zipwire.backends.FileReader` opens an archive on the local
filesystem through the same interface, so the same code works for local and
remote archives.  It accepts a path or a ``file://`` URI and needs no extra
dependency:

.. code-block:: python

   from zipwire import SyncRemoteZip
   from zipwire.backends import FileReader

   with SyncRemoteZip(FileReader("/path/to/archive.zip")) as rz:
       data = rz.read("path/to/file.txt")

   # Or from a file:// URI
   reader = FileReader.from_uri("file:///path/to/archive.zip")

Passwords, filenames, and comments
----------------------------------

Pass a bytes password to ``read`` or ``read_into`` for traditionally
encrypted ZIP entries. ``setpassword`` supplies a default for subsequent
reads; an explicit password overrides it:

.. code-block:: python

   with SyncRemoteZip(reader, metadata_encoding="cp1252") as rz:
       print(rz.comment)
       rz.setpassword(b"archive-password")
       data = rz.read("private.txt")
       with open("output.bin", "wb") as dest:
           rz.read_into("other.bin", dest, pwd=b"different-password")

``metadata_encoding`` is only needed for legacy archives whose filenames
use a code page other than CP437. UTF-8 filenames and valid Unicode Path
extra fields are decoded automatically. The same options work with
``AsyncRemoteZip`` and both wheel classes:

.. code-block:: python

   async with AsyncRemoteZip(reader, metadata_encoding="cp1252") as rz:
       data = await rz.read("private.txt", pwd=b"archive-password")

Stored, Deflate, Bzip2, ZIP-LZMA, and ZIP64 archives are supported.
Zstandard (including legacy method 20) requires Python's
``compression.zstd`` module, available from Python 3.14. AES encryption,
split archives, Deflate64, and PPMd are not supported.

Asynchronous usage
------------------

Async with aiohttp
^^^^^^^^^^^^^^^^^^

.. code-block:: python

   import asyncio

   from zipwire import AsyncRemoteZip
   from zipwire.backends import AiohttpReader


   async def main():
       reader = AiohttpReader("https://archive.example/data.zip")
       async with AsyncRemoteZip(reader) as rz:
           data = await rz.read("path/to/file.txt")
           print(data.decode())


   asyncio.run(main())

Async with httpx2
^^^^^^^^^^^^^^^^^

.. code-block:: python

   import asyncio

   from zipwire import AsyncRemoteZip
   from zipwire.backends import Httpx2AsyncReader


   async def main():
       reader = Httpx2AsyncReader("https://archive.example/data.zip")
       async with AsyncRemoteZip(reader) as rz:
           names = rz.namelist()
           print(names)


   asyncio.run(main())

Async local archive
^^^^^^^^^^^^^^^^^^^

:class:`~zipwire.backends.AsyncFileReader` reads a local archive and offloads
blocking file IO to a worker thread so it never stalls the event loop:

.. code-block:: python

   import asyncio

   from zipwire import AsyncRemoteZip
   from zipwire.backends import AsyncFileReader


   async def main():
       async with AsyncRemoteZip(AsyncFileReader("/path/to/archive.zip")) as rz:
           print(await rz.read("path/to/file.txt"))


   asyncio.run(main())

Async streaming to disk
^^^^^^^^^^^^^^^^^^^^^^^^

.. code-block:: python

   import asyncio

   from zipwire import AsyncRemoteZip
   from zipwire.backends import AiohttpReader


   async def main():
       reader = AiohttpReader("https://archive.example/large.zip")
       async with AsyncRemoteZip(reader) as rz:
           with open("output.bin", "wb") as f:
               await rz.read_into("big-file.bin", f)


   asyncio.run(main())

Wheel dist-info metadata
------------------------

``SyncRemoteWheel`` and ``AsyncRemoteWheel`` extend the base classes
with an adaptive tail fetch and a ``distinfolist()`` method.  The tail
size scales with the archive (at least 128 KiB, up to ~0.4% of the
file) so that dist-info entries are typically served from memory
without extra HTTP requests.

.. code-block:: python

   from zipwire import SyncRemoteWheel
   from zipwire.backends import Urllib3Reader

   url = "https://files.pythonhosted.org/.../requests-2.32.3-py3-none-any.whl"
   reader = Urllib3Reader(url)
   with SyncRemoteWheel(reader) as whl:
       for entry in whl.distinfolist():
           data = whl.read(entry)
           print(entry.filename, len(data))

Async variant:

.. code-block:: python

   import asyncio

   from zipwire import AsyncRemoteWheel
   from zipwire.backends import AiohttpReader


   async def main():
       reader = AiohttpReader(url)
       async with AsyncRemoteWheel(reader) as whl:
           for entry in whl.distinfolist():
               data = await whl.read(entry)
               print(entry.filename, len(data))


   asyncio.run(main())

Passing a pre-configured client
--------------------------------

All backends accept an optional pre-configured client or session:

.. code-block:: python

   import httpx2

   from zipwire import SyncRemoteZip
   from zipwire.backends import Httpx2SyncReader

   with httpx2.Client(headers={"Authorization": "Bearer token"}) as client:
       reader = Httpx2SyncReader(
           "https://archive.example/data.zip", client=client
       )
       with SyncRemoteZip(reader) as rz:
           data = rz.read("secret.txt")

Performance and async scheduling
--------------------------------

Central-directory data already in the initial tail is reused. Wheel
readers also serve complete cached members without additional requests.
``max_file_size`` rejects oversized entries before any member-specific
range request, using the metadata loaded when the archive was opened.

Async readers yield to the asyncio event loop between bounded decoding
steps, including when input comes from a cache. Decryption uses smaller
steps because traditional ZipCrypto executes in Python. These checkpoints
allow other tasks to run and deliver cancellation without waiting for an
entire entry to be decoded. Each individual codec call and destination
``write`` remains synchronous.

The repository includes a local benchmark:

.. code-block:: console

   .venv/bin/python benchmarks/bench_read.py

It reports extraction throughput, event-loop scheduling gaps, range
request counts, and peak traced allocations. It excludes archive creation
and network time. Source archives are allocated before memory tracing,
and native allocations that bypass Python's allocator are not included.
``--encrypted-zip PATH`` adds a single-entry ZipCrypto archive with the
test password ``test``. No external ZIP tool is needed to run the default
benchmark.
