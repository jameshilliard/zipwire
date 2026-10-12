Security
========

zipwire fetches data from remote servers and parses binary ZIP
structures.  When processing archives from untrusted sources, keep the
following risks in mind.

Zip bombs
---------

A *zip bomb* is a malicious archive containing entries that decompress
to a vastly larger size than their compressed representation, potentially
exhausting memory or disk space.

Both :meth:`~zipwire.SyncRemoteZip.read` and
:meth:`~zipwire.SyncRemoteZip.read_into` (and their async equivalents)
accept a ``max_file_size`` keyword argument that limits the uncompressed
size of a single entry:

.. code-block:: python

   from zipwire import SyncRemoteZip
   from zipwire.backends import Urllib3Reader

   reader = Urllib3Reader("https://example.com/untrusted.zip")
   with SyncRemoteZip(reader) as rz:
       for info in rz.infolist():
           data = rz.read(info, max_file_size=50 * 1024 * 1024)  # 50 MiB

When the limit is exceeded, :exc:`~zipwire.FileTooLarge` is raised.

Before member-specific range requests, the entry's declared ``file_size``
from the central directory is compared against the limit. Both methods
decode in bounded chunks and return at most the declared number of bytes,
matching stdlib ``zipfile``. A smaller declared size truncates the output;
the CRC must match the bytes actually returned. Stored entries can stop
reading at that size. Compressed entries continue decoding to the codec's
end marker or the declared compressed extent, discarding excess output in
bounded chunks to detect codec errors. Streamed responses are closed on
completion, failure, or cancellation. The output limit does not limit
the total work spent decoding and discarding excess data.

:meth:`read` still buffers the complete compressed and decompressed
contents in memory. Use :meth:`read_into` to stream large entries to a
destination. Decoded chunks are at most 2 MiB, including when decoding
cached input. This is a chunk-size bound, not a limit on total memory use.
Memory also includes input supplied by the backend, codec dictionaries,
archive metadata, the adaptive wheel tail cache, and destination buffers.
ZIP-LZMA dictionary size claims are clamped to 64 MiB.
``max_file_size`` limits returned output; it does not impose a memory
budget on those components.

You can also inspect entries before extracting them:

.. code-block:: python

   MAX_SIZE = 100 * 1024 * 1024  # 100 MiB

   with SyncRemoteZip(reader) as rz:
       for info in rz.infolist():
           if info.file_size > MAX_SIZE:
               print(f"Skipping {info.filename}: too large ({info.file_size} bytes)")
               continue
           data = rz.read(info)

Path traversal
--------------

ZIP archives can contain entries with absolute paths or ``../``
components (e.g. ``../../etc/passwd``).  zipwire does **not** extract
files to disk, so this is not a direct risk.  However, if you use
filenames from the archive to construct file paths, always sanitise them:

.. code-block:: python

   import os

   with SyncRemoteZip(reader) as rz:
       for info in rz.infolist():
           # Reject absolute paths and path traversal
           if os.path.isabs(info.filename) or ".." in info.filename.split("/"):
               print(f"Skipping suspicious path: {info.filename}")
               continue
           dest = os.path.join(output_dir, info.filename)
           # Verify the resolved path is inside output_dir
           if not os.path.realpath(dest).startswith(os.path.realpath(output_dir)):
               print(f"Skipping path traversal: {info.filename}")
               continue

Local header inconsistencies
----------------------------

Every ZIP entry is described twice: in the central directory and in
the local file header that precedes the file data.  A crafted archive
can name a different member in the two places, so tools that read the
local header (many extractors and scanners) disagree with tools that
read the central directory about what the archive contains.  This
matters for supply-chain workflows such as wheel inspection even
though zipwire never writes files to disk.

When an entry is read, zipwire compares the local header's filename
against the central directory name, like CPython's ``zipfile`` does in
``ZipFile.open()``, and raises :exc:`~zipwire.BadZipFile` when they
differ.

Server trust
------------

zipwire sends HTTP requests to a URL you provide.  The server controls
what data is returned.  A malicious server could return crafted responses
designed to exploit ZIP parsing bugs, cause excessive memory use, or
return different data for different range requests.

- Only fetch archives from servers you trust.
- Use HTTPS to prevent tampering in transit.
- Consider setting ``max_file_size`` even for trusted servers as a
  safety net.
