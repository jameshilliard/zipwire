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

:meth:`~zipwire.AsyncRemoteZip.read_into` accepts async destinations such as
``anyio.AsyncFile``. Install AnyIO separately for this example. Each write is
awaited before extraction continues:

.. code-block:: python

   import asyncio

   import anyio

   from zipwire import AsyncRemoteZip
   from zipwire.backends import Httpx2AsyncReader


   async def main():
       reader = Httpx2AsyncReader("https://archive.example/large.zip")
       async with AsyncRemoteZip(reader) as rz:
           async with await anyio.open_file("output.bin", "wb") as f:
               await rz.read_into("big-file.bin", f)


   asyncio.run(main())

Synchronous destinations such as ``io.BytesIO`` remain supported. Their
``write()`` calls run on the event-loop thread, so use an async destination
for blocking disk or network writes.

The destination remains open after ``read_into`` returns. The caller must
flush, close, or commit it as appropriate. Treat partial output as invalid
if extraction raises or is cancelled: CRC verification is complete only
when ``read_into`` returns successfully.

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
