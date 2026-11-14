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
entries.  Decoded output is passed to the destination in chunks of at most
2 MiB, so a highly compressible entry cannot inflate into a single large
allocation.  Partial writes are retried, so raw unbuffered streams work as
destinations.  A destination's ``write()`` must return the number of bytes
accepted, or ``None`` for the whole chunk:

.. code-block:: python

   from zipwire import SyncRemoteZip
   from zipwire.backends import RequestsReader

   reader = RequestsReader("https://archive.example/large.zip")
   with SyncRemoteZip(reader) as rz:
       with open("output.bin", "wb") as f:
           rz.read_into("big-file.bin", f)

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

:meth:`~zipwire.AsyncRemoteZip.read_into` also accepts asynchronous
writable objects, such as ``anyio.AsyncFile``.  When ``write()`` returns
an awaitable, zipwire waits for that write to finish before it reads more
input, so a slow destination throttles the download.  Partial writes are
retried in both modes, and a byte count that is not an integer, or is zero,
negative, or oversized, raises ``OSError``.  Any object with an awaitable
``write()`` works; the example uses anyio:

.. code-block:: python

   import asyncio

   import anyio

   from zipwire import AsyncRemoteZip
   from zipwire.backends import AiohttpReader


   async def main():
       reader = AiohttpReader("https://archive.example/large.zip")
       async with AsyncRemoteZip(reader) as rz:
           async with await anyio.open_file("output.bin", "wb") as f:
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

Legacy filename encodings
-------------------------

Entry names are decoded as UTF-8 when the general purpose flag bit 11
is set and as CP437 otherwise, as in the standard library's ``zipfile``.
``SyncRemoteZip`` and ``AsyncRemoteZip`` accept a
``metadata_encoding`` argument with the same semantics as
``zipfile.ZipFile`` for entries without the flag:

.. code-block:: python

   from zipwire import SyncRemoteZip
   from zipwire.backends import Urllib3Reader

   reader = Urllib3Reader("https://archive.example/legacy.zip")
   with SyncRemoteZip(reader, metadata_encoding="cp1252") as rz:
       print(rz.namelist())

The wheel classes do not expose the argument: they always decode entry
names as UTF-8, as PEP 427 requires, even when the flag is missing.

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
